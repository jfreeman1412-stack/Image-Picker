"""Face detection + embedding wrapper around InsightFace.

The model is loaded once and reused. InsightFace's `buffalo_l` pack gives us
detection, alignment, and a 512-d ArcFace embedding in one shot — which is what
we want for downstream clustering.

Phase 8: GPU acceleration. We request CUDA first with a 2 GB VRAM cap so the
detector is a good citizen on a shared GPU (LM Studio etc. live on the same
3060 here). Falling back to CPU is automatic in two ways:
  1. If CUDA fails to init at session-create time, ORT silently drops it
     and `get_providers()` returns only `['CPUExecutionProvider']` —
     we detect that after `prepare()` and emit a clear warning.
  2. As a hard guard, we wrap the FaceAnalysis construction so a hard
     exception (e.g. ORT throws because cuDNN DLLs are missing) still
     yields a working CPU detector with a warning logged.
"""
import logging
import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import List

import numpy as np

logger = logging.getLogger(__name__)


def _add_cuda_dll_search_dirs() -> None:
    """Ensure the Windows loader can find CUDA 12.x runtime DLLs (cudart,
    cublas, cudnn*, etc.) before ORT tries to load onnxruntime_providers_cuda.dll.

    Why this is necessary: the CUDA 12.6 installer doesn't always prepend
    its bin/ to PATH when an older CUDA is already present (observed on
    this machine: PATH still pointed exclusively at v11.2\\bin after
    installing v12.6 on top, leading to LoadLibrary error 126 even though
    every required DLL was physically present in v12.6\\bin). The Windows
    DLL loader only searches PATH + system dirs by default;
    os.add_dll_directory (Python 3.8+, Windows) adds a directory to this
    process's search path independent of env vars, so the operator
    doesn't have to hand-edit PATH.

    Picks the highest-installed CUDA 12.x. No-op on non-Windows or when
    no v12.x install is present (downstream CPU fallback handles that).
    Module-level so it runs before insightface's lazy ORT import.
    """
    if sys.platform != "win32":
        return
    root = Path(r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA")
    if not root.is_dir():
        return
    candidates = sorted(
        (p for p in root.iterdir()
         if p.is_dir() and p.name.startswith("v12.")),
        key=lambda p: p.name, reverse=True,
    )
    for cuda_dir in candidates:
        bin_dir = cuda_dir / "bin"
        if bin_dir.is_dir():
            added = False
            try:
                os.add_dll_directory(str(bin_dir))
                added = True
            except OSError as exc:
                logger.warning(
                    "[gpu] Could not add CUDA DLL dir %s: %s", bin_dir, exc,
                )
            # Also prepend to PATH for the *current process*. Belt and
            # suspenders: os.add_dll_directory doesn't always reach native
            # libs that LoadLibraryEx their own dependencies (ORT's provider
            # DLL is one of them); PATH is the universal Windows fallback
            # and ORT honours it. We don't touch the system PATH — this is
            # process-local.
            bin_str = str(bin_dir)
            current = os.environ.get("PATH", "")
            if bin_str.lower() not in current.lower():
                os.environ["PATH"] = bin_str + os.pathsep + current
            if added:
                # logger.warning (not info) so it survives uvicorn's default
                # log-level filtering — we want this visible at boot.
                logger.warning(
                    "[gpu] Wired CUDA DLL search dir into this process: %s",
                    bin_dir,
                )
                return  # highest version wins


_add_cuda_dll_search_dirs()


DET_SCORE_THRESHOLD = 0.5

# 2 GB per-session cap on GPU memory. ORT applies this to each CUDA session
# created under buffalo_l (detection / recognition / genderage / landmark).
# In practice each session only grows as needed, so total VRAM stays well
# under the cap-sum — but the ceiling protects us from spikes if cuDNN
# decides to pre-allocate a large convolution workspace.
_GPU_MEM_LIMIT_BYTES = 2 * 1024 * 1024 * 1024

_CUDA_PROVIDER = (
    "CUDAExecutionProvider",
    {
        "device_id": 0,
        "gpu_mem_limit": _GPU_MEM_LIMIT_BYTES,
        # kSameAsRequested grows the BFC arena conservatively — better for
        # cohabiting with other GPU workloads than the default doubling.
        "arena_extend_strategy": "kSameAsRequested",
        # HEURISTIC instead of the default EXHAUSTIVE: skip the ~8 s
        # per-session conv-algo benchmark that fires on first inference.
        # Our workload runs each model on a per-image basis (no thousands
        # of identical inferences to amortize over), so the marginally
        # sub-optimal kernel selection costs far less than the warmup
        # would. Saves ~6–8 s of cold-start per pipeline run.
        "cudnn_conv_algo_search": "HEURISTIC",
    },
)


def _session_providers(app) -> list[str]:
    """Best-effort introspection of which ORT providers the *active*
    detection session actually has. `get_providers()` reflects ORT's
    post-init pruning, so this is the source of truth for 'did CUDA load?'.
    """
    try:
        return list(app.models["detection"].session.get_providers())
    except Exception:  # noqa: BLE001 — defensive: model dict shape varies
        return ["<unknown>"]


def _build_providers() -> list:
    """Return the providers list to hand to FaceAnalysis. Only includes the
    CUDA provider if THIS onnxruntime build supports it — keeps the log
    message accurate on a CPU-only ORT install (e.g. CI) instead of yelling
    'CUDA unavailable' when CUDA was never on the menu."""
    try:
        import onnxruntime as ort
        available = ort.get_available_providers()
    except Exception:  # noqa: BLE001 — defensive on import
        available = []
    if "CUDAExecutionProvider" in available:
        return [_CUDA_PROVIDER, "CPUExecutionProvider"]
    return ["CPUExecutionProvider"]


@lru_cache(maxsize=1)
def get_detector():
    """Load the InsightFace model once per process. GPU first, CPU fallback.

    First load downloads ~300MB to ~/.insightface/ which is expected.
    """
    from insightface.app import FaceAnalysis

    providers = _build_providers()
    cuda_requested = any(
        (isinstance(p, tuple) and p[0] == "CUDAExecutionProvider")
        or p == "CUDAExecutionProvider"
        for p in providers
    )

    try:
        app = FaceAnalysis(name="buffalo_l", providers=providers)
    except Exception as exc:  # noqa: BLE001 — broad: ORT raises odd types here
        logger.warning(
            "[gpu] FaceAnalysis init failed (%s); retrying CPU-only.", exc,
        )
        app = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])

    app.prepare(ctx_id=0, det_size=(640, 640))

    active = _session_providers(app)
    on_gpu = bool(active) and active[0] == "CUDAExecutionProvider"
    if on_gpu:
        # warning level so it clears uvicorn's default INFO filter — at-a-
        # glance visibility for "is GPU active?" is the whole point.
        logger.warning(
            "[gpu] InsightFace loaded with GPU acceleration (providers=%s, "
            "VRAM cap=%d MB).",
            active, _GPU_MEM_LIMIT_BYTES // (1024 * 1024),
        )
    elif cuda_requested:
        # CUDA was on the menu but ORT silently dropped it (commonly cuDNN
        # DLLs not on PATH for this Python process). Loud warning.
        logger.warning(
            "[gpu] InsightFace fell back to CPU (providers=%s). If GPU was "
            "expected: confirm CUDA 12.x runtime + cuDNN 9.x are installed "
            "and that the cuDNN bin/ dir is on PATH for the uvicorn process.",
            active,
        )
    else:
        # Honest log: no GPU on this ORT build, no fallback to apologize for.
        logger.info(
            "[gpu] InsightFace loaded CPU-only (providers=%s; CUDA not "
            "available in this onnxruntime build).",
            active,
        )
    return app


# ── Per-pipeline-run provider sanity check ──────────────────────────────────
# Catches silent-fallback scenarios where the session-create succeeded with
# CUDA, but at first detection something is off (very rare — included as a
# belt-and-suspenders against future ORT changes / driver issues). Pipeline
# calls `arm_run_provider_check()` at run start; the next `detect_faces`
# call logs exactly once and clears the flag.

_run_provider_check_pending = False


def arm_run_provider_check() -> None:
    """Signal that the next detect_faces() call should log which provider
    is actually being used. Called once per pipeline run."""
    global _run_provider_check_pending
    _run_provider_check_pending = True


def detect_faces(image_path: Path) -> List[dict]:
    """Detect every face in `image_path`.

    Returns a list of dicts:
        {
            "bbox": [x, y, w, h],   # ints
            "embedding": np.ndarray (float32, shape (512,)),  # L2-normalized
            "det_score": float,
            "age": float | None,            # InsightFace genderage estimate
            "yaw": float | None,            # head pose yaw, degrees
            "pitch": float | None,          # head pose pitch, degrees
            "face_area_ratio": float | None, # bbox area / image area
        }
    Returns [] if the image cannot be loaded.
    """
    from app.services.image_io import read_bgr

    img = read_bgr(image_path)
    if img is None:
        logger.warning("Could not load image %s", image_path)
        return []

    img_h, img_w = img.shape[:2]
    img_area = float(img_h * img_w) if img_h and img_w else 0.0

    app = get_detector()

    global _run_provider_check_pending
    if _run_provider_check_pending:
        active = _session_providers(app)
        on_gpu = bool(active) and active[0] == "CUDAExecutionProvider"
        # warning level for visibility through uvicorn's default INFO filter.
        logger.warning(
            "[gpu] first detection this run: %s (providers=%s).",
            "GPU" if on_gpu else "CPU",
            active,
        )
        _run_provider_check_pending = False

    faces = app.get(img)
    out: List[dict] = []
    for f in faces:
        if float(f.det_score) < DET_SCORE_THRESHOLD:
            continue
        x1, y1, x2, y2 = f.bbox.astype(int)
        bbox_w, bbox_h = int(x2 - x1), int(y2 - y1)
        age = getattr(f, "age", None)

        # InsightFace buffalo_l exposes .pose as [pitch, yaw, roll] in degrees.
        # Older versions / edge cases: missing → None and downstream pose check
        # conservatively treats it as a fail.
        pose = getattr(f, "pose", None)
        pitch_val = yaw_val = None
        if pose is not None:
            try:
                pitch_val = float(pose[0])
                yaw_val = float(pose[1])
            except (TypeError, ValueError, IndexError):
                pitch_val = yaw_val = None

        area_ratio = (bbox_w * bbox_h) / img_area if img_area else None

        out.append({
            "bbox": [int(x1), int(y1), bbox_w, bbox_h],
            "embedding": np.asarray(f.normed_embedding, dtype=np.float32),
            "det_score": float(f.det_score),
            "age": float(age) if age is not None else None,
            "yaw": yaw_val,
            "pitch": pitch_val,
            "face_area_ratio": float(area_ratio) if area_ratio is not None else None,
        })
    return out
