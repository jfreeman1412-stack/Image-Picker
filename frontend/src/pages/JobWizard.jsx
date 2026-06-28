import { useState, useEffect } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import FolderBrowser from '../components/FolderBrowser.jsx';
import { pickRepresentativeTeam } from '../utils/pickRepresentativeTeam.js';

// 2026-06-28 wizard-mixed-structure fix.
//
// Soft "looks like a color check" hint substrings. Case-insensitive
// substring match. Never auto-excludes — purely visual annotation in
// the skip-list. Structural signal (lacks-chosen-subfolder) is what
// actually drives the skip; this list only draws the operator's eye.
//
// Keep this TIGHT — generic terms ("photos", "card") would false-flag
// legitimate team names ("Color Crew", "Wildcards").
const SOFT_HINT_SUBSTRINGS = [
  'color', 'swatch', 'chart', 'check',
  'calibration', 'grey card', 'gray card',
];
function looksLikeColorCheck(folderName) {
  const n = (folderName || '').toLowerCase();
  return SOFT_HINT_SUBSTRINGS.some(p => n.includes(p));
}

// Compute the zero-images guard severity from the per-team peeks. Two-tier:
//   HARD (red, requires explicit second-click): populated==0, OR
//         (populated==1 AND total≥3). Catches the swatch-hijack: 1 swatch
//         populated, N real teams empty.
//   SOFT (yellow, informational): populated<50% AND total≥4. Catches
//         "maybe you picked the wrong subfolder" without false-alarming
//         small jobs with a couple legitimately-pending teams.
//   none: majority populated, OR total<4 (small jobs — operator eyeballs).
function computeZeroImagesGuard(teamPeeks, imageSubfolder) {
  if (!teamPeeks || teamPeeks.length === 0) return { level: 'none' };
  const counts = teamPeeks.map(p => ({
    name: p.name,
    images: imageSubfolder
      ? (p.subfolders.find(s => s.name === imageSubfolder)?.image_count ?? 0)
      : (p.image_count ?? 0),
  }));
  const populated = counts.filter(c => c.images > 0);
  const empty = counts.filter(c => c.images === 0);
  const total = counts.length;
  if (populated.length === 0
      || (populated.length === 1 && total >= 3)) {
    return { level: 'hard', populated, empty, total };
  }
  if (populated.length < total / 2 && total >= 4) {
    return { level: 'soft', populated, empty, total };
  }
  return { level: 'none', populated, empty, total };
}

/**
 * New-job wizard. Phase 9 reorders the flow to:
 *   1. Job name
 *   2. Roster Y/N + optional CSV upload (stashed locally, POSTed after create)
 *   3. Job folder (browse modal or paste path) → /peek root
 *   4. Lines / Teams / Single → /peek first line if needed
 *   5. Verify team folders
 *   6. Where the images live (team-folder root or named subfolder)
 *   7. Confirm & create (POST /jobs, then auto-POST roster, then poll ingest)
 *
 * Internal /peek logic mirrors the previous wizard exactly — only the
 * surrounding step UX changed. Resilient-fetch pattern from Phase 5
 * everywhere: check res.ok, never let an error body land in state.
 */

const STEP_KEYS = ['name', 'roster', 'folder', 'structure', 'teams', 'subfolder', 'create'];
const STEP_LABELS = {
  name: 'Job name',
  roster: 'Roster',
  folder: 'Job folder',
  structure: 'Structure',
  teams: 'Verify teams',
  subfolder: 'Images in…',
  create: 'Create',
};

async function peek(path) {
  const res = await fetch('/api/jobs/peek', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  });
  if (!res.ok) {
    let msg;
    try { msg = await res.text(); } catch { msg = `HTTP ${res.status}`; }
    throw new Error(msg);
  }
  return res.json();
}


export default function JobWizard() {
  const nav = useNavigate();
  // Phase C.2: when rendered at /job/:id/import-images, the wizard runs in
  // "import mode" — it skips name + roster and ingests into the existing job.
  const { id: targetJobId } = useParams();
  const importMode = !!targetJobId;

  const [step, setStep] = useState(importMode ? 'folder' : 'name');
  const [name, setName] = useState('');
  const [useRoster, setUseRoster] = useState(null);          // true | false | null
  const [rosterFile, setRosterFile] = useState(null);        // File obj
  const [rootPath, setRootPath] = useState('');
  const [rootPeek, setRootPeek] = useState(null);
  const [hasLines, setHasLines] = useState(null);            // true | false | 'single'
  const [teamFolders, setTeamFolders] = useState([]);
  // 2026-06-28 wizard-mixed-structure: was firstTeamPeek (pre-fix the
  // wizard sampled only teamFolders[0] for structure detection, which let
  // a non-team first folder (color swatch) hijack the operator's subfolder
  // choice → silent under-ingest of every real team). Now we peek ALL
  // team folders in parallel and pickRepresentativeTeam() chooses the
  // structurally richest one for the 55/56 logic to read.
  const [representativePeek, setRepresentativePeek] = useState(null);
  // All per-team peeks. Drives the skip-list and zero-images guard in
  // step 7 (each entry has subfolders + image_count, so we can derive
  // "would this team ingest 0 images under the chosen imageSubfolder?").
  const [teamPeeks, setTeamPeeks] = useState([]);
  const [imageSubfolder, setImageSubfolder] = useState(null);
  // Hard-confirm acknowledgment for the HARD zero-images guard. Mirrors
  // the destructive run-all confirm pattern in JobDetail.jsx — operator
  // must explicitly click "I understand, create anyway" before the
  // Create button activates when the guard fires HARD.
  const [hardConfirmAck, setHardConfirmAck] = useState(false);
  const [autoRun, setAutoRun] = useState(true);

  const [showFolderPicker, setShowFolderPicker] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [rosterUploadWarning, setRosterUploadWarning] = useState(null);

  // Job creation + ingest polling state.
  const [creatingJobId, setCreatingJobId] = useState(null);
  const [ingest, setIngest] = useState(null);

  const reset = () => setError(null);

  // Once a job has been created, poll ingest status; nav to the job page
  // when it finishes (after firing the roster upload if one was selected).
  useEffect(() => {
    if (!creatingJobId) return;
    let stop = false;
    const tick = async () => {
      try {
        const r = await fetch(`/api/jobs/${creatingJobId}/ingest-status`);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const s = await r.json();
        if (stop) return;
        setIngest(s);
        if (s.status === 'done') {
          nav(`/job/${creatingJobId}`);
        } else if (s.status === 'error') {
          setError(s.error || 'Ingest failed.');
        } else {
          setTimeout(tick, 1000);
        }
      } catch (e) {
        if (!stop) setTimeout(tick, 1500);
      }
    };
    tick();
    return () => { stop = true; };
  }, [creatingJobId, nav]);

  // ── Step transitions ─────────────────────────────────────────────────
  const goRoster = () => { reset(); setStep('roster'); };
  const goFolder = () => { reset(); setStep('folder'); };

  /** /peek the root path. */
  const onFolderConfirm = async () => {
    reset();
    if (!rootPath.trim()) { setError('Pick a folder first.'); return; }
    setBusy(true);
    try {
      const data = await peek(rootPath);
      setRootPeek(data);
      setStep('structure');
    } catch (e) { setError(String(e.message || e)); }
    finally { setBusy(false); }
  };

  /** lines / teams / single decision. */
  const chooseStructure = async (choice) => {
    reset();
    setHasLines(choice);
    setBusy(true);
    try {
      if (choice === true) {
        const firstLine = rootPeek.subfolders[0];
        if (!firstLine) throw new Error('No line folders found at root.');
        const linePath = `${rootPath}\\${firstLine.name}`;
        const inside = await peek(linePath);
        setTeamFolders(inside.subfolders);
        setStep('teams');
      } else if (choice === false) {
        setTeamFolders(rootPeek.subfolders);
        setStep('teams');
      } else {
        // 'single' — the whole root is one team. teamPeeks holds the one
        // peek so the skip-list/zero-images guard logic doesn't have to
        // special-case single-team mode.
        const inside = await peek(rootPath);
        const teamName = rootPeek.path.split(/[\\/]/).pop();
        const singlePeek = { ...inside, name: teamName };
        setTeamFolders([{
          name: teamName,
          subfolder_count: 0,
          image_count: rootPeek.image_count,
          raw_count: rootPeek.raw_count,
        }]);
        setTeamPeeks([singlePeek]);
        setRepresentativePeek(singlePeek);
        setStep('subfolder');
      }
    } catch (e) { setError(String(e.message || e)); }
    finally { setBusy(false); }
  };

  /** From teams confirmation → /peek EVERY team folder in parallel,
   *  pick the structurally richest one as the representative for the
   *  subfolder step. Browser caps concurrent fetches per origin at 6-8
   *  so a 30-team job runs in ~5 batches; expect ~1.5-2s on UNC.
   *  Spinner stays up via setBusy until all peeks resolve.
   *
   *  Why all folders, not a sample: the bug we're fixing is exactly
   *  "wrong folder happened to sort first." Sampling 6 evenly-spaced
   *  folders would still miss a swatch if it landed between samples.
   *  Cost is bounded by browser concurrency, not folder count.
   *
   *  Falls back to teamFolders[0]-only behavior when picker returns
   *  null (defensive — shouldn't happen given teamFolders is non-empty
   *  by step 5 gating). */
  const confirmTeams = async () => {
    reset(); setBusy(true);
    try {
      const linePrefix = hasLines === true
        ? `${rootPath}\\${rootPeek.subfolders[0].name}\\`
        : `${rootPath}\\`;
      const peekResults = await Promise.all(
        teamFolders.map(async (t) => {
          try {
            const inside = await peek(`${linePrefix}${t.name}`);
            return { ...inside, name: t.name };
          } catch (e) {
            // Single-folder peek failure is non-fatal — that folder just
            // doesn't get peeked. Log + return null; filtered below so it
            // doesn't pollute the picker / skip-list / guard.
            console.warn(`[wizard] peek failed for ${t.name}:`, e);
            return null;
          }
        }),
      );
      const peeks = peekResults.filter(Boolean);
      if (peeks.length === 0) {
        throw new Error('No team folders could be peeked.');
      }
      setTeamPeeks(peeks);
      const rep = pickRepresentativeTeam(peeks) || peeks[0];
      setRepresentativePeek(rep);
      setStep('subfolder');
    } catch (e) { setError(String(e.message || e)); }
    finally { setBusy(false); }
  };

  const chooseImageLocation = (subfolderName) => {
    setImageSubfolder(subfolderName);
    // Reset hard-confirm ack — if the operator goes back and re-picks the
    // subfolder, they must re-acknowledge the guard for the new choice.
    setHardConfirmAck(false);
    setStep('create');
  };

  // Final: create job, then upload roster (if any), then nav once ingest done.
  const create = async () => {
    reset();
    setBusy(true);
    setRosterUploadWarning(null);
    try {
      const res = await fetch(
        importMode ? `/api/jobs/${targetJobId}/import-images` : '/api/jobs',
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            ...(importMode ? {} : { name }),
            root_path: rootPath,
            has_lines: hasLines === true,
            image_subfolder_name: imageSubfolder,
            auto_run: autoRun,
          }),
        });
      if (!res.ok) {
        let msg;
        try { msg = await res.text(); } catch { msg = `HTTP ${res.status}`; }
        throw new Error(msg);
      }
      const data = await res.json();
      setIngest({ status: 'pending', progress: 0, total: data.ingest_total || 0 });
      setCreatingJobId(data.job_id);

      // Upload roster in the background. Don't block ingest polling; the
      // CSV upload is fast and the job page works either way.
      if (rosterFile) {
        const fd = new FormData();
        fd.append('file', rosterFile);
        try {
          const r = await fetch(`/api/jobs/${data.job_id}/roster`, { method: 'POST', body: fd });
          if (!r.ok) {
            setRosterUploadWarning(
              `Roster upload failed (HTTP ${r.status}). You can re-upload from the job page.`,
            );
          }
        } catch (e) {
          setRosterUploadWarning(
            `Roster upload failed (${e.message}). You can re-upload from the job page.`,
          );
        }
      }
    } catch (e) {
      setError(String(e.message || e));
      setBusy(false);
    }
  };

  // ── Header step indicator ────────────────────────────────────────────
  // Import mode reuses only the back half — hide the name/roster steps.
  const stepKeysShown = importMode
    ? STEP_KEYS.filter(k => k !== 'name' && k !== 'roster')
    : STEP_KEYS;
  const stepIndex = stepKeysShown.indexOf(step);

  // ── Render ───────────────────────────────────────────────────────────
  return (
    <div className="page">
      <header className="row-between">
        <div>
          <Link to="/">← All jobs</Link>
          <h1>{importMode ? 'Import images' : 'New job'}</h1>
        </div>
      </header>

      <ol className="wizard-steps">
        {stepKeysShown.map((k, i) => (
          <li key={k} className={k === step ? 'active' : i < stepIndex ? 'done' : ''}>
            {i + 1}. {STEP_LABELS[k]}
          </li>
        ))}
      </ol>

      {error && <div className="card error">{error}</div>}

      {/* Step 1: Job name */}
      {step === 'name' && (
        <section className="card">
          <h2>Job name</h2>
          <p className="muted">Used as the job title throughout the app.</p>
          <div className="form-row">
            <input
              autoFocus
              placeholder="e.g. Princeton Spring 2026"
              value={name}
              onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter' && name.trim()) goRoster(); }}
              style={{ flex: 1 }}
            />
            <button disabled={!name.trim()} onClick={goRoster}>Next →</button>
          </div>
        </section>
      )}

      {/* Step 2: Roster Y/N + upload */}
      {step === 'roster' && (
        <section className="card">
          <h2>Roster CSV</h2>
          <p className="muted">
            Optional. Two positional columns, no header: <code>player-name,team</code>.
            If you have one, the app will use it to flag wrong-team photos after the
            pipeline runs each team.
          </p>
          <div className="wizard-choices">
            <button
              className={useRoster === true ? 'primary' : ''}
              onClick={() => setUseRoster(true)}
            >
              Yes — I have a roster CSV
            </button>
            <button
              className={useRoster === false ? 'primary' : ''}
              onClick={() => { setUseRoster(false); setRosterFile(null); }}
            >
              No — skip
            </button>
          </div>
          {useRoster === true && (
            <div className="form-row" style={{ flexDirection: 'column', alignItems: 'stretch', marginTop: 12 }}>
              <input
                type="file"
                accept=".csv,text/csv"
                onChange={(e) => setRosterFile(e.target.files?.[0] || null)}
              />
              {rosterFile && (
                <p className="muted" style={{ marginTop: 4, fontSize: 12 }}>
                  Selected: <b>{rosterFile.name}</b> ({Math.round(rosterFile.size / 1024)} KB).
                  Uploaded automatically after the job is created.
                </p>
              )}
            </div>
          )}
          <div className="actions" style={{ marginTop: 14 }}>
            <button className="ghost" onClick={() => setStep('name')}>← Back</button>
            <button
              disabled={useRoster === null || (useRoster === true && !rosterFile)}
              onClick={goFolder}
            >
              Next →
            </button>
          </div>
        </section>
      )}

      {/* Step 3: Job folder */}
      {step === 'folder' && (
        <section className="card">
          <h2>Job folder</h2>
          <p className="muted">
            Top-level folder containing every team (or every line of teams). UNC
            paths work: <code>\\server\share\Photo Day\…</code>.
          </p>
          <div className="form-row">
            <input
              placeholder="\\192.168.1.134\Sportsline Office\Photo Day\…"
              value={rootPath}
              onChange={(e) => setRootPath(e.target.value)}
              style={{ flex: 1 }}
            />
            <button onClick={() => setShowFolderPicker(true)}>Browse…</button>
          </div>
          <div className="actions">
            <button
              className="ghost"
              onClick={() => importMode ? nav(`/job/${targetJobId}`) : setStep('roster')}
            >
              ← {importMode ? 'Cancel' : 'Back'}
            </button>
            <button disabled={!rootPath.trim() || busy} onClick={onFolderConfirm}>
              {busy ? 'Inspecting…' : 'Next →'}
            </button>
          </div>
          {showFolderPicker && (
            <FolderBrowser
              title="Pick the job folder"
              initialPath={rootPath || ''}
              onPick={(p) => setRootPath(p)}
              onClose={() => setShowFolderPicker(false)}
            />
          )}
        </section>
      )}

      {/* Step 4: Structure */}
      {step === 'structure' && rootPeek && (
        <section className="card">
          <h2>Structure</h2>
          <p>
            Inside <code>{rootPath}</code> there are <b>{rootPeek.subfolders.length}</b>{' '}
            subfolders. What do they represent?
          </p>
          <div className="wizard-choices">
            <button disabled={busy} onClick={() => chooseStructure(true)}>
              Lines (each subfolder is a line; line folders contain teams)
            </button>
            <button disabled={busy} onClick={() => chooseStructure(false)}>
              Teams (each subfolder is a team)
            </button>
            <button disabled={busy} onClick={() => chooseStructure('single')}>
              Single team (the whole job folder is one team)
            </button>
          </div>
          <div className="actions">
            <button className="ghost" onClick={() => setStep('folder')}>← Back</button>
          </div>
        </section>
      )}

      {/* Step 5: Verify teams */}
      {step === 'teams' && (
        <section className="card">
          <h2>Verify team folders</h2>
          <p>These look like the team folders {hasLines === true ? '(under the first line)' : ''}:</p>
          <ul className="folder-list">
            {teamFolders.map((f) => (
              <li key={f.name}>
                <b>{f.name}</b>
                <span className="muted"> · {f.subfolder_count} subfolders · {f.image_count} images · {f.raw_count} raws</span>
              </li>
            ))}
          </ul>
          <p className="muted">{teamFolders.length} teams total.</p>
          <div className="actions">
            <button className="ghost" onClick={() => setStep('structure')}>← Back</button>
            <button disabled={busy} onClick={confirmTeams}>
              {busy
                ? `Inspecting ${teamFolders.length} team folder${teamFolders.length === 1 ? '' : 's'}…`
                : 'Looks right →'}
            </button>
          </div>
        </section>
      )}

      {/* Step 6: Image subfolder
          2026-06-08: surface the renditions-in-subfolder case. Job 55
          missed because the operator clicked "team-folder root" without
          noticing the root had 0 supported-format files (only Canon CR3
          RAWs, which ingest_folder skips). Three fixes:
            1. Symmetric image counts on every button (root included).
            2. Recommended marker on the subfolder with the most images,
               when it beats the root count.
            3. Warning banner + root button disabled when root has 0
               supported images AND a subfolder has > 0 — so the only
               clickable option leads to a non-empty ingest. */}
      {step === 'subfolder' && representativePeek && (() => {
        const rootImages = representativePeek.image_count || 0;
        const rootRaws = representativePeek.raw_count || 0;
        // Best subfolder = highest image_count, ties broken by name asc.
        const subs = [...(representativePeek.subfolders || [])]
          .sort((a, b) => (b.image_count - a.image_count) || a.name.localeCompare(b.name));
        const bestSub = subs.find((s) => s.image_count > 0) || null;
        // Recommended marker fires when the best subfolder has more
        // images than the root — that's the clearest signal that the
        // root pick won't ingest anything.
        const recommendedName = (bestSub && bestSub.image_count > rootImages)
          ? bestSub.name : null;
        // Root pick is disabled when it would ingest 0 files AND a
        // subfolder offers a deliverable count. Keeps the "RAWs only at
        // root + no rendition subfolder anywhere" case clickable so the
        // wizard doesn't trap the user — they can still create the job
        // and see the empty result if that's what they want.
        const rootDisabled = rootImages === 0 && bestSub !== null;
        const showRawWarning = rootImages === 0 && rootRaws > 0;

        return (
        <section className="card">
          <h2>Where are the images?</h2>
          <p>
            Looking inside <code>{representativePeek.name}</code>: found{' '}
            <b>{rootImages}</b> images and <b>{rootRaws}</b> raws
            in the team-folder root, plus <b>{representativePeek.subfolders.length}</b> subfolders.
          </p>
          {teamPeeks.length > 1 && (
            <p className="muted" style={{ fontSize: '0.9em' }}>
              (Showing structure from <code>{representativePeek.name}</code> as the
              representative folder. The skip-list on the next step will tell
              you which of the {teamPeeks.length} team folders lack your chosen
              subfolder.)
            </p>
          )}
          {showRawWarning && (
            <p className="warn" style={{ background: '#fff3cd', padding: 8, borderRadius: 4 }}>
              ⚠ The team-folder root has <b>0 supported-format images</b>
              {' '}({rootRaws} RAW files, which can't be ingested directly).
              {bestSub
                ? <> Pick <b>"{bestSub.name}"</b> below — it has the renditions.</>
                : <> No subfolder with deliverable images found either —
                  this folder structure may need conversion before ingest.</>}
            </p>
          )}
          <p className="warn">
            The pattern you pick here applies to <b>every</b> team folder in the job.
          </p>
          <div className="wizard-choices">
            <button
              onClick={() => chooseImageLocation(null)}
              disabled={rootDisabled}
              title={rootDisabled
                ? 'Disabled: the team-folder root has 0 supported-format images. Pick a subfolder.'
                : undefined}
              style={rootDisabled ? { opacity: 0.5, cursor: 'not-allowed' } : undefined}
            >
              Images are in the team-folder root
              <span className="muted"> ({rootImages} images)</span>
              {recommendedName === null && rootImages > 0 && (
                <span style={{ marginLeft: 6, color: '#0a7d2a' }}> ★ Recommended</span>
              )}
            </button>
            {subs.map((sub) => (
              <button key={sub.name} onClick={() => chooseImageLocation(sub.name)}>
                Images are in subfolder "{sub.name}"
                <span className="muted"> ({sub.image_count} images)</span>
                {recommendedName === sub.name && (
                  <span style={{ marginLeft: 6, color: '#0a7d2a' }}> ★ Recommended</span>
                )}
              </button>
            ))}
          </div>
          <div className="actions">
            <button
              className="ghost"
              onClick={() => setStep(hasLines === 'single' ? 'structure' : 'teams')}
            >
              ← Back
            </button>
          </div>
        </section>
        );
      })()}

      {/* Step 7: Create + progress
          2026-06-28 wizard-mixed-structure: this step now derives a
          skip-list + zero-images guard from teamPeeks so the operator
          confirms BEFORE creating instead of discovering after ingest
          that only the swatch populated and every real team is empty.
          The guard is the load-bearing safety net for the silent
          under-ingest path; the skip-list is informational. */}
      {step === 'create' && (() => {
        // Derive skip-list: any team whose peek lacks the chosen
        // imageSubfolder. When imageSubfolder is null (root pick),
        // nothing is skipped at ingest by the backend — but the
        // zero-images guard catches that case.
        const willSkip = imageSubfolder
          ? teamPeeks.filter(p =>
              !p.subfolders.some(s => s.name === imageSubfolder),
            )
          : [];
        const willIngest = teamPeeks.filter(p => !willSkip.includes(p));
        const guard = computeZeroImagesGuard(teamPeeks, imageSubfolder);
        // The Create button is gated when the HARD guard fires AND the
        // operator hasn't explicitly acknowledged. Matches the
        // destructive-run-all confirm pattern.
        const hardGated = guard.level === 'hard' && !hardConfirmAck;
        return (
        <section className="card">
          <h2>{importMode ? 'Confirm and import' : 'Confirm and create'}</h2>
          {!creatingJobId ? (
            <>
              <ul>
                {!importMode && <li>Job: <b>{name}</b></li>}
                {!importMode && (
                  <li>Roster: {useRoster ? <b>{rosterFile?.name || 'will upload'}</b> : <span className="muted">(none)</span>}</li>
                )}
                <li>Folder: <code>{rootPath}</code></li>
                <li>Structure: {hasLines === true ? 'lines → teams' : hasLines === 'single' ? 'single team' : 'teams'}</li>
                <li>
                  Teams: <b>{teamFolders.length}</b> total
                  {willSkip.length > 0 && (
                    <> &middot; <b>{willIngest.length}</b> will ingest
                    &middot; <b>{willSkip.length}</b> will be skipped</>
                  )}
                </li>
                <li>Images in: {imageSubfolder ? <code>{imageSubfolder}/</code> : 'team-folder root'}</li>
              </ul>

              {/* Skip-list — informational. Surfaces folders the backend
                  will skip at ingest (existing skipped_teams mechanism,
                  jobs.py:209/222/272). Soft color-check hint annotated. */}
              {willSkip.length > 0 && (
                <div className="card" style={{ background: '#f4f4f4', padding: 10, marginBottom: 8 }}>
                  <p style={{ marginTop: 0 }}>
                    These <b>{willSkip.length}</b> folder{willSkip.length === 1 ? '' : 's'}{' '}
                    will be skipped at ingest because they lack the{' '}
                    <code>{imageSubfolder}</code> subfolder:
                  </p>
                  <ul style={{ marginBottom: 0 }}>
                    {willSkip.map(p => (
                      <li key={p.name}>
                        <b>{p.name}</b>
                        {looksLikeColorCheck(p.name) && (
                          <span className="muted" style={{ marginLeft: 6 }}>
                            (looks like a color check)
                          </span>
                        )}
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              {/* Zero-images guard — HARD case (red). The load-bearing
                  safety net: this catches the swatch-hijack where the
                  operator's subfolder choice would silently zero-ingest
                  every real team. Requires explicit second click. */}
              {guard.level === 'hard' && (
                <div
                  className="card"
                  style={{
                    background: '#fce4e4', borderLeft: '4px solid #b00020',
                    padding: 12, marginBottom: 8,
                  }}
                >
                  <p style={{ marginTop: 0 }}>
                    <b>⚠ Stop — this looks wrong.</b>
                  </p>
                  <p>
                    Only <b>{guard.populated.length}</b> of <b>{guard.total}</b>{' '}
                    team folder{guard.total === 1 ? '' : 's'} will ingest images
                    {guard.populated.length > 0 && (
                      <> (<b>{guard.populated.map(p => p.name).join(', ')}</b>)</>
                    )}.
                    The other <b>{guard.empty.length}</b> will be empty.
                  </p>
                  <p>
                    {imageSubfolder
                      ? <>Did you pick the wrong subfolder? Real team folders
                        usually have <code>Adjusted</code> or similar.</>
                      : <>Did you mean to pick a subfolder like <code>Adjusted</code>{' '}
                        instead of the team-folder root? Most renditions live
                        in a subfolder.</>}
                  </p>
                  <label style={{ display: 'block', marginTop: 8 }}>
                    <input
                      type="checkbox"
                      checked={hardConfirmAck}
                      onChange={(e) => setHardConfirmAck(e.target.checked)}
                    />
                    {' '}I understand, create anyway
                  </label>
                </div>
              )}

              {/* SOFT case (yellow): informational, no extra click. */}
              {guard.level === 'soft' && (
                <div
                  className="card"
                  style={{
                    background: '#fff3cd', borderLeft: '4px solid #b58900',
                    padding: 10, marginBottom: 8,
                  }}
                >
                  <p style={{ marginTop: 0, marginBottom: 0 }}>
                    <b>{guard.populated.length} of {guard.total}</b> team
                    folders will ingest images. Verify this is correct.
                  </p>
                </div>
              )}

              <label className="auto-run-toggle">
                <input
                  type="checkbox"
                  checked={autoRun}
                  onChange={(e) => setAutoRun(e.target.checked)}
                />
                Start processing automatically when import finishes
              </label>
              <div className="actions">
                <button className="ghost" onClick={() => setStep('subfolder')}>← Back</button>
                <button disabled={busy || hardGated} onClick={create}>
                  {busy
                    ? (importMode ? 'Importing…' : 'Creating…')
                    : (importMode ? 'Import images' : 'Create job')}
                </button>
              </div>
            </>
          ) : ingest?.status === 'error' ? (
            <div>
              <div className="card error">
                Ingest failed: {ingest.error || error || 'unknown error'}
              </div>
              <div className="actions">
                <button
                  className="ghost"
                  onClick={() => {
                    setCreatingJobId(null); setIngest(null); reset(); setBusy(false);
                  }}
                >
                  ← Back to wizard
                </button>
              </div>
            </div>
          ) : (
            <div className="ingest-progress">
              <p><b>{importMode ? 'Importing images…' : 'Creating job…'}</b></p>
              <div className="progress-bar">
                <div
                  className="progress-fill"
                  style={{
                    width: ingest && ingest.total
                      ? `${Math.round((ingest.progress / ingest.total) * 100)}%`
                      : '0%',
                  }}
                />
              </div>
              <p className="muted">
                {ingest?.progress || 0} of {ingest?.total || 0} teams
                {ingest?.current_team && <> · currently: <b>{ingest.current_team}</b></>}
              </p>
              {rosterUploadWarning && (
                <p className="warn" style={{ marginTop: 8 }}>{rosterUploadWarning}</p>
              )}
            </div>
          )}
        </section>
        );
      })()}
    </div>
  );
}
