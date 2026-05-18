import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';

export default function SessionList() {
  const [sessions, setSessions] = useState([]);
  const [name, setName] = useState('');
  const [folder, setFolder] = useState('');

  const load = () => fetch('/api/sessions').then(r => r.json()).then(setSessions);
  useEffect(() => { load(); }, []);

  const create = async () => {
    const res = await fetch('/api/sessions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, folder }),
    });
    if (!res.ok) {
      alert(await res.text());
      return;
    }
    setName(''); setFolder('');
    load();
  };

  return (
    <div className="page">
      <header><h1>Player Sort</h1></header>

      <section className="card">
        <h2>New session</h2>
        <div className="form-row">
          <input placeholder="Session name (e.g. Lincoln Mustangs 2026)"
                 value={name} onChange={e => setName(e.target.value)} />
          <input placeholder="Folder path (e.g. C:\Shoots\Lincoln)"
                 value={folder} onChange={e => setFolder(e.target.value)} />
          <button onClick={create} disabled={!name || !folder}>Create</button>
        </div>
      </section>

      <section className="card">
        <h2>Sessions</h2>
        {sessions.length === 0 && <p className="muted">No sessions yet.</p>}
        <ul className="session-list">
          {sessions.map(s => (
            <li key={s.id}>
              <Link to={`/session/${s.id}`}>{s.name}</Link>
              <span className="muted"> · {s.image_count} images · {s.status}</span>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
