import { useState, useEffect } from 'react';
import { Link, useNavigate } from 'react-router-dom';

const STEPS = ['Pick folder', 'Lines?', 'Teams', 'Images?', 'Confirm'];

async function peek(path) {
  const res = await fetch('/api/jobs/peek', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export default function JobWizard() {
  const nav = useNavigate();
  const [step, setStep] = useState(0);
  const [rootPath, setRootPath] = useState('');
  const [jobName, setJobName] = useState('');
  const [rootPeek, setRootPeek] = useState(null);
  const [hasLines, setHasLines] = useState(null);           // true | false | "single"
  const [teamFolders, setTeamFolders] = useState([]);       // list of {name, ...} from peek of root or first line
  const [firstTeamPeek, setFirstTeamPeek] = useState(null);
  const [imageSubfolder, setImageSubfolder] = useState(null); // null = images at team root; else subfolder name
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [creatingJobId, setCreatingJobId] = useState(null);
  const [ingest, setIngest] = useState(null); // {status,progress,total,current_team,error,skipped_teams}

  const reset = () => setError(null);

  // Poll ingest status once a job has been created.
  useEffect(() => {
    if (!creatingJobId) return;
    let stop = false;
    const tick = async () => {
      try {
        const s = await fetch(`/api/jobs/${creatingJobId}/ingest-status`).then(r => r.json());
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

  // Step 1: peek the root folder
  const doPeekRoot = async () => {
    reset(); setBusy(true);
    try {
      const data = await peek(rootPath);
      setRootPeek(data);
      if (!jobName) {
        const last = rootPath.replace(/[\\/]+$/, '').split(/[\\/]/).pop();
        setJobName(last || 'New job');
      }
    } catch (e) { setError(String(e.message || e)); }
    finally { setBusy(false); }
  };

  // Move from step 1 → 2
  const toStep2 = () => { setStep(1); };

  // Step 2 choices: lines or teams or single
  const chooseStructure = async (choice) => {
    setHasLines(choice);
    reset(); setBusy(true);
    try {
      if (choice === true) {
        // Peek the first line folder to find team folders.
        const firstLine = rootPeek.subfolders[0];
        if (!firstLine) throw new Error('No line folders found at root.');
        const linePath = `${rootPath}\\${firstLine.name}`;
        const inside = await peek(linePath);
        setTeamFolders(inside.subfolders);
        setStep(2);
      } else if (choice === false) {
        // Root subfolders are team folders.
        setTeamFolders(rootPeek.subfolders);
        setStep(2);
      } else if (choice === 'single') {
        // Whole job is one team. Skip team confirmation, go straight to image-location step
        // using the root folder as the team folder.
        setTeamFolders([{ name: rootPeek.path.split(/[\\/]/).pop(), subfolder_count: 0, image_count: rootPeek.image_count, raw_count: rootPeek.raw_count }]);
        const inside = await peek(rootPath);
        setFirstTeamPeek({ ...inside, name: rootPeek.path.split(/[\\/]/).pop() });
        setStep(3);
      }
    } catch (e) { setError(String(e.message || e)); }
    finally { setBusy(false); }
  };

  // Step 3 → 4: peek into the first team folder
  const confirmTeams = async () => {
    reset(); setBusy(true);
    try {
      const firstTeam = teamFolders[0];
      let teamPath;
      if (hasLines === true) {
        teamPath = `${rootPath}\\${rootPeek.subfolders[0].name}\\${firstTeam.name}`;
      } else {
        teamPath = `${rootPath}\\${firstTeam.name}`;
      }
      const inside = await peek(teamPath);
      setFirstTeamPeek({ ...inside, name: firstTeam.name });
      setStep(3);
    } catch (e) { setError(String(e.message || e)); }
    finally { setBusy(false); }
  };

  // Step 4 → 5
  const chooseJpgLocation = (subfolderName) => {
    setImageSubfolder(subfolderName);
    setStep(4);
  };

  // Final submit — kicks off async ingest, then the poll effect takes over.
  const create = async () => {
    reset(); setBusy(true);
    try {
      const res = await fetch('/api/jobs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: jobName,
          root_path: rootPath,
          has_lines: hasLines === true,
          image_subfolder_name: imageSubfolder,
        }),
      });
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      setIngest({ status: 'pending', progress: 0, total: data.ingest_total || 0 });
      setCreatingJobId(data.job_id);
    } catch (e) {
      setError(String(e.message || e));
      setBusy(false);
    }
  };

  return (
    <div className="page">
      <header className="row-between">
        <div>
          <Link to="/">← All jobs</Link>
          <h1>New job</h1>
        </div>
      </header>

      <ol className="wizard-steps">
        {STEPS.map((s, i) => (
          <li key={s} className={i === step ? 'active' : i < step ? 'done' : ''}>
            {i + 1}. {s}
          </li>
        ))}
      </ol>

      {error && <div className="card error">{error}</div>}

      {step === 0 && (
        <section className="card">
          <h2>Step 1 — Job folder</h2>
          <p className="muted">Paste the full path to the top-level job folder.</p>
          <div className="form-row">
            <input
              placeholder="D:\Shoots\Lincoln Spring 2026"
              value={rootPath}
              onChange={e => setRootPath(e.target.value)}
              style={{ flex: 1 }}
            />
            <button onClick={doPeekRoot} disabled={!rootPath || busy}>
              {busy ? 'Looking…' : 'Peek'}
            </button>
          </div>

          {rootPeek && (
            <div className="peek-summary">
              <p>
                Found <b>{rootPeek.subfolders.length}</b> subfolders,{' '}
                <b>{rootPeek.image_count}</b> images (JPG/PNG/TIFF), <b>{rootPeek.raw_count}</b> raw files
                at the top level.
              </p>
              <ul className="folder-list">
                {rootPeek.subfolders.map(f => (
                  <li key={f.name}>
                    <b>{f.name}</b>
                    <span className="muted"> · {f.subfolder_count} subfolders · {f.image_count} images · {f.raw_count} raws</span>
                  </li>
                ))}
              </ul>
              <div className="form-row">
                <label style={{ flex: 1 }}>
                  Job name
                  <input value={jobName} onChange={e => setJobName(e.target.value)} />
                </label>
                <button onClick={toStep2} disabled={!jobName}>Next →</button>
              </div>
            </div>
          )}
        </section>
      )}

      {step === 1 && (
        <section className="card">
          <h2>Step 2 — Structure</h2>
          <p>What do the subfolders inside <code>{rootPath}</code> represent?</p>
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
          <p><button className="ghost" onClick={() => setStep(0)}>← Back</button></p>
        </section>
      )}

      {step === 2 && (
        <section className="card">
          <h2>Step 3 — Confirm team folders</h2>
          <p>These look like the team folders {hasLines ? '(under the first line)' : ''}:</p>
          <ul className="folder-list">
            {teamFolders.map(f => (
              <li key={f.name}>
                <b>{f.name}</b>
                <span className="muted"> · {f.subfolder_count} subfolders · {f.image_count} JPGs · {f.raw_count} raws</span>
              </li>
            ))}
          </ul>
          <p className="muted">{teamFolders.length} teams total.</p>
          <div className="actions">
            <button className="ghost" onClick={() => setStep(1)}>← Back</button>
            <button disabled={busy} onClick={confirmTeams}>Looks right →</button>
          </div>
        </section>
      )}

      {step === 3 && firstTeamPeek && (
        <section className="card">
          <h2>Step 4 — Where are the images?</h2>
          <p>
            Looking inside <code>{firstTeamPeek.name}</code>: found{' '}
            <b>{firstTeamPeek.image_count}</b> images (JPG/PNG/TIFF) and <b>{firstTeamPeek.raw_count}</b> raws
            in the team folder root, plus <b>{firstTeamPeek.subfolders.length}</b> subfolders.
          </p>
          <p className="warn">
            The pattern you pick here applies to <b>every</b> team folder in the job.
            If teams have inconsistent structures, import each line separately.
          </p>
          <div className="wizard-choices">
            <button onClick={() => chooseJpgLocation(null)}>
              Images are in the team folder root
            </button>
            {firstTeamPeek.subfolders.map(sub => (
              <button key={sub.name} onClick={() => chooseJpgLocation(sub.name)}>
                Images are in subfolder "{sub.name}"
                <span className="muted"> ({sub.image_count} images)</span>
              </button>
            ))}
          </div>
          <p><button className="ghost" onClick={() => setStep(hasLines === 'single' ? 1 : 2)}>← Back</button></p>
        </section>
      )}

      {step === 4 && (
        <section className="card">
          <h2>Step 5 — Confirm and create</h2>

          {!creatingJobId ? (
            <>
              <p>Will create:</p>
              <ul>
                <li>Job: <b>{jobName}</b></li>
                <li>Root: <code>{rootPath}</code></li>
                <li>Structure: {hasLines === true ? 'lines → teams' : hasLines === 'single' ? 'single team' : 'teams'}</li>
                <li>Teams: <b>{teamFolders.length}</b></li>
                <li>Images found in: {imageSubfolder ? <code>{imageSubfolder}/</code> : 'team folder root'}</li>
              </ul>
              <div className="actions">
                <button className="ghost" onClick={() => setStep(3)}>← Back</button>
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
                <button className="ghost" onClick={() => {
                  setCreatingJobId(null); setIngest(null); reset(); setBusy(false);
                }}>← Back to wizard</button>
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
            </div>
          )}
        </section>
      )}
    </div>
  );
}
