import { useState, useEffect, useRef } from 'react';
import FolderBrowser from './FolderBrowser.jsx';

function fmtDuration(secs) {
  if (secs == null) return '—';
  if (secs < 60) return `${secs}s`;
  const m = Math.floor(secs / 60);
  const s = secs % 60;
  return s ? `${m}m ${s}s` : `${m}m`;
}

export default function ExportModal({ job, onClose }) {
  const [mode, setMode] = useState('copy');
  const [overwrite, setOverwrite] = useState(true);
  // 2026-06-02: per-export rename toggle. Default OFF — feature soaks
  // before becoming the default. When ON, filenames derive from each
  // cluster's display_label() + role suffix or sequence number.
  const [renameByPlayer, setRenameByPlayer] = useState(false);
  // 2026-09-08: per-export buddy-split toggle. Default OFF — off-mode is
  // byte-identical to today's export. When ON, images with 2+ detected
  // faces whose best role is individual/buddy get pulled out of
  // To_be_Cropped/ into a sibling Buddies/ tree that mirrors the same
  // team subfolder structure. Team/pano roles stay put.
  const [splitBuddies, setSplitBuddies] = useState(false);
  // 2026-10-07: per-JOB Sytist passcodes option, switched on in the Player
  // roster screen. ON → export also writes the Sytist import CSV. uploadExt
  // is the extension the photos will have when uploaded (cropping may make PNGs).
  const sytistPasscodes = !!job.sytist_passcodes;
  // Most shoots' cropped photos are uploaded as PNGs (same name).
  const [uploadExt, setUploadExt] = useState('.png');
  // 2026-10-08: rebuild the Sytist CSV from the final (cropped) folder.
  const [finalFolder, setFinalFolder] = useState('');
  const [showFinalPicker, setShowFinalPicker] = useState(false);
  const [folderBusy, setFolderBusy] = useState(false);
  const [folderResult, setFolderResult] = useState(null);
  const [folderError, setFolderError] = useState(null);
  const [phase, setPhase] = useState('form'); // form | running | done | error
  const [status, setStatus] = useState(null); // /export-status payload
  const [error, setError] = useState(null);
  const pollRef = useRef(null);

  // Default = legacy <root>_sorted; user can override via Browse or by typing.
  const legacyDefault = job.root_path.replace(/[\\/]+$/, '') + '_sorted';
  const [destination, setDestination] = useState(legacyDefault);
  const [showPicker, setShowPicker] = useState(false);

  useEffect(() => () => clearInterval(pollRef.current), []);

  const poll = () => {
    pollRef.current = setInterval(async () => {
      const s = await fetch(`/api/jobs/${job.id}/export-status`).then(r => r.json());
      setStatus(s);
      if (s.status === 'done') {
        clearInterval(pollRef.current);
        setPhase('done');
      } else if (s.status === 'error') {
        clearInterval(pollRef.current);
        setError(s.error || 'Export failed.');
        setPhase('error');
      }
    }, 1000);
  };

  const buildFromFolder = async () => {
    setFolderBusy(true); setFolderError(null); setFolderResult(null);
    try {
      const res = await fetch(`/api/sytist/jobs/${job.id}/csv-from-folder`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ folder: finalFolder }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail?.message || `Failed (${res.status})`);
      setFolderResult(body);
    } catch (e) {
      setFolderError(e.message);
    } finally {
      setFolderBusy(false);
    }
  };

  const finalFolderSection = (
    <div className="form-row" style={{ flexDirection: 'column', alignItems: 'stretch', marginTop: 8 }}>
      <b>Build the passcode CSV from the final folder</b>
      <span className="muted" style={{ fontSize: 13 }}>
        After cropping, point this at the folder you upload to Sytist. Files are
        matched to the last export by name (any file type), and the CSV is saved
        in that folder.
      </span>
      <div className="actions" style={{ gap: 6, marginTop: 6 }}>
        <input style={{ flex: 1 }} value={finalFolder} placeholder="Final folder"
               onChange={e => setFinalFolder(e.target.value)} />
        <button className="ghost" onClick={() => setShowFinalPicker(true)}>Browse…</button>
        <button onClick={buildFromFolder} disabled={folderBusy || !finalFolder.trim()}>
          {folderBusy ? 'Building…' : 'Build CSV'}
        </button>
      </div>
      {folderError && <p className="error">{folderError}</p>}
      {folderResult && (
        <p>
          Saved <code>{folderResult.path}</code> ({folderResult.photo_rows} photos,{' '}
          {folderResult.players} players, {folderResult.group_photos} group photos).
          {folderResult.not_in_export > 0 && (
            <span className="warn">
              {' '}{folderResult.not_in_export} file(s) weren't in the last export and got
              no passcode (e.g. {folderResult.not_in_export_examples.slice(0, 3).join(', ')}).
            </span>
          )}
          {folderResult.duplicate_names > 0 && (
            <span className="warn">
              {' '}{folderResult.duplicate_names} file name(s) are used more than once (e.g.{' '}
              {folderResult.duplicate_examples.slice(0, 3).join(', ')}). Sytist matches photos by
              name across the whole gallery, so rename them before uploading.
            </span>
          )}
          {folderResult.unassigned_files > 0 && (
            <span className="warn">
              {' '}{folderResult.unassigned_files} photo(s) aren't matched to a roster player.
            </span>
          )}
          {folderResult.team_photos_missing.length > 0 && (
            <span className="muted">
              {' '}Team photos not in this folder (upload them too):{' '}
              {folderResult.team_photos_missing.slice(0, 6).join(', ')}
              {folderResult.team_photos_missing.length > 6 && '…'}
            </span>
          )}
        </p>
      )}
      {showFinalPicker && (
        <FolderBrowser
          title="Choose the final folder"
          initialPath={finalFolder || legacyDefault}
          onPick={(p) => setFinalFolder(p)}
          onClose={() => setShowFinalPicker(false)}
        />
      )}
    </div>
  );

  const submit = async () => {
    setError(null);
    // Only send destination_path when the user actually customized it.
    // Sending legacyDefault verbatim is harmless but unnecessary.
    const body = { mode, overwrite };
    if (renameByPlayer) body.rename_by_player = true;
    if (splitBuddies) body.split_buddies = true;
    if (sytistPasscodes && uploadExt) body.sytist_upload_ext = uploadExt;
    const trimmed = (destination || '').trim();
    if (trimmed && trimmed !== legacyDefault) body.destination_path = trimmed;
    const res = await fetch(`/api/jobs/${job.id}/export`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      // 409 (output exists / already running), 400, etc. — synchronous.
      let msg;
      try { msg = (await res.json()).detail; } catch { msg = await res.text(); }
      setError(typeof msg === 'string' ? msg : 'Could not start export.');
      return;
    }
    const data = await res.json();
    setStatus({ status: 'exporting', progress: 0, total: data.export_total || 0 });
    setPhase('running');
    poll();
  };

  const pct = status && status.total
    ? Math.round((status.progress / status.total) * 100) : 0;
  const busy = phase === 'running';

  return (
    <div className="modal-backdrop" onClick={busy ? undefined : onClose}>
      <div className="modal-panel" onClick={e => e.stopPropagation()}>
        <h2>Export job</h2>

        {phase === 'form' && (
          <>
            <div className="form-row" style={{ flexDirection: 'column', alignItems: 'stretch' }}>
              <label style={{ marginBottom: 4 }}>Destination</label>
              <div className="actions" style={{ gap: 8 }}>
                <input
                  type="text"
                  value={destination}
                  onChange={(e) => setDestination(e.target.value)}
                  style={{ flex: 1 }}
                  placeholder="Where to copy / move the sorted output"
                />
                <button className="ghost" onClick={() => setShowPicker(true)}>Browse…</button>
              </div>
              <p className="muted" style={{ marginTop: 4, fontSize: 12 }}>
                Defaults to <code>{legacyDefault}</code> (a sibling of the job folder).
              </p>
            </div>
            <div className="form-row">
              <label>
                <input type="radio" name="mode" value="copy"
                       checked={mode === 'copy'} onChange={() => setMode('copy')} />
                Copy (originals stay)
              </label>
              <label>
                <input type="radio" name="mode" value="move"
                       checked={mode === 'move'} onChange={() => setMode('move')} />
                Move (originals are moved out)
              </label>
            </div>
            <div className="form-row">
              <label>
                <input type="checkbox" checked={overwrite}
                       onChange={e => setOverwrite(e.target.checked)} />
                Overwrite if the destination already exists
              </label>
            </div>
            <div className="form-row">
              <label>
                <input type="checkbox" checked={renameByPlayer}
                       onChange={e => setRenameByPlayer(e.target.checked)} />
                Rename files by player (clustered photos use the player's name; buddy
                photos get a copy per kid; orphan/calibration images keep camera names)
              </label>
            </div>
            <div className="form-row">
              <label>
                <input type="checkbox" checked={splitBuddies}
                       onChange={e => setSplitBuddies(e.target.checked)} />
                Split buddy photos into a separate <code>Buddies/</code> tree
                (images with 2+ detected faces get pulled out of{' '}
                <code>To_be_Cropped/</code> so individuals and buddies can be
                cropped in two clean passes; team/pano photos are unaffected)
              </label>
            </div>
            <div className="form-row" style={{ flexDirection: 'column', alignItems: 'stretch' }}>
              {sytistPasscodes ? (
                <span>
                  <b>Sytist passcodes are on for this job.</b> The export also writes{' '}
                  <code>{'<job name>'}_sytist_passcodes.csv</code> to import into a Preset
                  Passcode Photos gallery after uploading the photos.
                </span>
              ) : (
                <span className="muted" style={{ fontSize: 13 }}>
                  Sytist passcodes are off for this job. Turn them on in Player roster.
                </span>
              )}
              {sytistPasscodes && (
                <label style={{ marginTop: 6 }}>
                  Photos will be uploaded to Sytist as{' '}
                  <select value={uploadExt} onChange={e => setUploadExt(e.target.value)}>
                    <option value="">the same file type as exported</option>
                    <option value=".png">.png</option>
                    <option value=".jpg">.jpg</option>
                  </select>
                </label>
              )}
              {sytistPasscodes && finalFolderSection}
            </div>
            {error && <p className="error">{error}</p>}
            <div className="actions">
              <button className="ghost" onClick={onClose}>Cancel</button>
              <button onClick={submit} disabled={!destination.trim()}>Export</button>
            </div>
            {showPicker && (
              <FolderBrowser
                title="Choose export destination"
                initialPath={destination || legacyDefault}
                onPick={(p) => setDestination(p)}
                onClose={() => setShowPicker(false)}
              />
            )}
          </>
        )}

        {phase === 'running' && (
          <div className="ingest-progress">
            <p><b>Exporting…</b> {status?.current_team && <>copying <b>{status.current_team}</b></>}</p>
            <div className="progress-bar">
              <div className="progress-fill" style={{ width: `${pct}%` }} />
            </div>
            <p className="muted">
              {status?.progress || 0} of {status?.total || 0} files · {pct}%
              {' · '}elapsed {fmtDuration(status?.elapsed_seconds)}
              {status?.eta_seconds != null
                ? ` · ~${fmtDuration(status.eta_seconds)} left`
                : ' · estimating…'}
            </p>
            <p className="muted">Leave this open — copying over the network can take a while.</p>
          </div>
        )}

        {phase === 'done' && status?.result && (
          <>
            <p>
              <b>Done.</b> Exported {status.result.files_copied} files across{' '}
              {status.result.team_count} teams.{' '}
              {status.result.files_skipped_rejected} rejected images skipped.
            </p>
            {status.result.sytist_csv && (
              <p>
                Sytist passcode CSV: <code>{status.result.sytist_csv.path}</code>{' '}
                ({status.result.sytist_csv.players} players,{' '}
                {status.result.sytist_csv.photo_rows} photos,{' '}
                {status.result.sytist_csv.group_photos} group photos).
                {status.result.sytist_csv.duplicate_names > 0 && (
                  <span className="warn">
                    {' '}{status.result.sytist_csv.duplicate_names} file name(s) are used more than once (e.g.{' '}
                    {status.result.sytist_csv.duplicate_examples.slice(0, 3).join(', ')}). Sytist matches photos by
                    name across the whole gallery, so rename them before uploading.
                  </span>
                )}
                {status.result.sytist_csv.unassigned_files > 0 && (
                  <span className="warn">
                    {' '}{status.result.sytist_csv.unassigned_files} photo(s) aren't
                    matched to a roster player and got no passcode (e.g.{' '}
                    {status.result.sytist_csv.unassigned_examples.slice(0, 3).join(', ')}).
                  </span>
                )}
                {status.result.sytist_csv.sync && (
                  status.result.sytist_csv.sync.error
                    ? <span className="warn"> Couldn't sync families from Sytist: {status.result.sytist_csv.sync.error}</span>
                    : <span className="muted">
                        {' '}Synced Sytist families first ({status.result.sytist_csv.sync.matched_players} matched
                        {status.result.sytist_csv.sync.unmatched > 0 &&
                          `, ${status.result.sytist_csv.sync.unmatched} not on the roster`}).
                      </span>
                )}
              </p>
            )}
            {status.result.sytist_csv && finalFolderSection}
            {status.result.sessions_skipped?.length > 0 && (
              <p className="warn">
                Skipped: {status.result.sessions_skipped
                  .map(s => `${s.name} (${s.reason})`).join(', ')}
              </p>
            )}
            <div className="actions"><button onClick={onClose}>Close</button></div>
          </>
        )}

        {phase === 'error' && (
          <>
            <p className="error">Export failed: {error}</p>
            <div className="actions">
              <button className="ghost" onClick={() => { setPhase('form'); setError(null); }}>
                Back
              </button>
              <button onClick={onClose}>Close</button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
