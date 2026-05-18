import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';

const FLAG_LABELS = {
  team_pick_not_smiling: 'Team pick not smiling',
  pano_pick_smiling: 'Pano pick smiling',
  no_clean_pano_pose: 'No clean pano pose',
  no_pano_candidate: 'No pano candidate',
  no_single_face_images: 'No single-face images',
  ambiguous_copyright: 'Ambiguous copyright',
  outlier_high: 'Outlier image count (high)',
  outlier_low: 'Outlier image count (low)',
  no_team_pick: 'No team pick',
  no_pano_pick: 'No pano pick',
  coach_no_solo_image: 'Coach has no solo image',
  empty_cluster: 'Empty cluster',
};

export default function Settings() {
  const [vis, setVis] = useState(null);
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    fetch('/api/settings/flag-visibility').then(r => r.json()).then(setVis);
  }, []);

  const toggle = (flag) => {
    setVis(v => ({ ...v, [flag]: !v[flag] }));
    setSaved(false);
  };

  const save = async () => {
    setBusy(true);
    const updated = await fetch('/api/settings/flag-visibility', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(vis),
    }).then(r => r.json());
    setVis(updated);
    setBusy(false);
    setSaved(true);
  };

  if (!vis) return <div className="page">Loading…</div>;

  return (
    <div className="page">
      <header className="row-between">
        <div>
          <Link to="/">← All jobs</Link>
          <h1>Settings</h1>
          <p className="muted">
            Uncheck a flag to hide it from cluster review. The pipeline still
            computes it — this only changes what you see.
          </p>
        </div>
      </header>

      <section className="card">
        <h2>Visible review flags</h2>
        <div className="flag-list">
          {Object.keys(vis).map(flag => (
            <label key={flag} className="flag-row">
              <input
                type="checkbox"
                checked={vis[flag]}
                onChange={() => toggle(flag)}
              />
              <span>{FLAG_LABELS[flag] || flag}</span>
              {!vis[flag] && <span className="muted"> — hidden</span>}
            </label>
          ))}
        </div>
        <div className="actions" style={{ marginTop: 16 }}>
          <button className="primary-cta" onClick={save} disabled={busy}>
            {busy ? 'Saving…' : 'Save'}
          </button>
          {saved && <span className="muted" style={{ alignSelf: 'center' }}>Saved ✓</span>}
        </div>
      </section>
    </div>
  );
}
