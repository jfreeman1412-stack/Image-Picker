import { useState, useEffect } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import FolderBrowser from '../components/FolderBrowser.jsx';

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

  const [step, setStep] = useState('name');
  const [name, setName] = useState('');
  const [useRoster, setUseRoster] = useState(null);          // true | false | null
  const [rosterFile, setRosterFile] = useState(null);        // File obj
  const [rootPath, setRootPath] = useState('');
  const [rootPeek, setRootPeek] = useState(null);
  const [hasLines, setHasLines] = useState(null);            // true | false | 'single'
  const [teamFolders, setTeamFolders] = useState([]);
  const [firstTeamPeek, setFirstTeamPeek] = useState(null);
  const [imageSubfolder, setImageSubfolder] = useState(null);
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
        // 'single' — the whole root is one team.
        const inside = await peek(rootPath);
        const teamName = rootPeek.path.split(/[\\/]/).pop();
        setTeamFolders([{
          name: teamName,
          subfolder_count: 0,
          image_count: rootPeek.image_count,
          raw_count: rootPeek.raw_count,
        }]);
        setFirstTeamPeek({ ...inside, name: teamName });
        setStep('subfolder');
      }
    } catch (e) { setError(String(e.message || e)); }
    finally { setBusy(false); }
  };

  /** From teams confirmation → /peek first team to find image subfolders. */
  const confirmTeams = async () => {
    reset(); setBusy(true);
    try {
      const firstTeam = teamFolders[0];
      const teamPath = hasLines === true
        ? `${rootPath}\\${rootPeek.subfolders[0].name}\\${firstTeam.name}`
        : `${rootPath}\\${firstTeam.name}`;
      const inside = await peek(teamPath);
      setFirstTeamPeek({ ...inside, name: firstTeam.name });
      setStep('subfolder');
    } catch (e) { setError(String(e.message || e)); }
    finally { setBusy(false); }
  };

  const chooseImageLocation = (subfolderName) => {
    setImageSubfolder(subfolderName);
    setStep('create');
  };

  // Final: create job, then upload roster (if any), then nav once ingest done.
  const create = async () => {
    reset();
    setBusy(true);
    setRosterUploadWarning(null);
    try {
      const res = await fetch('/api/jobs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name,
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
  const visibleSteps = STEP_KEYS.filter(k => !(k === 'subfolder' && hasLines === 'single' && step !== 'subfolder'));
  // (we always show 'subfolder' once we're on/past it — it's a real step)
  const stepIndex = STEP_KEYS.indexOf(step);

  // ── Render ───────────────────────────────────────────────────────────
  return (
    <div className="page">
      <header className="row-between">
        <div>
          <Link to="/">← All jobs</Link>
          <h1>New job</h1>
        </div>
      </header>

      <ol className="wizard-steps">
        {STEP_KEYS.map((k, i) => (
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
            <button className="ghost" onClick={() => setStep('roster')}>← Back</button>
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
            <button disabled={busy} onClick={confirmTeams}>Looks right →</button>
          </div>
        </section>
      )}

      {/* Step 6: Image subfolder */}
      {step === 'subfolder' && firstTeamPeek && (
        <section className="card">
          <h2>Where are the images?</h2>
          <p>
            Looking inside <code>{firstTeamPeek.name}</code>: found{' '}
            <b>{firstTeamPeek.image_count}</b> images and <b>{firstTeamPeek.raw_count}</b> raws
            in the team-folder root, plus <b>{firstTeamPeek.subfolders.length}</b> subfolders.
          </p>
          <p className="warn">
            The pattern you pick here applies to <b>every</b> team folder in the job.
          </p>
          <div className="wizard-choices">
            <button onClick={() => chooseImageLocation(null)}>
              Images are in the team-folder root
            </button>
            {firstTeamPeek.subfolders.map((sub) => (
              <button key={sub.name} onClick={() => chooseImageLocation(sub.name)}>
                Images are in subfolder "{sub.name}"
                <span className="muted"> ({sub.image_count} images)</span>
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
      )}

      {/* Step 7: Create + progress */}
      {step === 'create' && (
        <section className="card">
          <h2>Confirm and create</h2>
          {!creatingJobId ? (
            <>
              <ul>
                <li>Job: <b>{name}</b></li>
                <li>Roster: {useRoster ? <b>{rosterFile?.name || 'will upload'}</b> : <span className="muted">(none)</span>}</li>
                <li>Folder: <code>{rootPath}</code></li>
                <li>Structure: {hasLines === true ? 'lines → teams' : hasLines === 'single' ? 'single team' : 'teams'}</li>
                <li>Teams: <b>{teamFolders.length}</b></li>
                <li>Images in: {imageSubfolder ? <code>{imageSubfolder}/</code> : 'team-folder root'}</li>
              </ul>
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
                <button disabled={busy} onClick={create}>
                  {busy ? 'Creating…' : 'Create job'}
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
              <p><b>Creating job…</b></p>
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
      )}
    </div>
  );
}
