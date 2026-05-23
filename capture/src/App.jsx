// Phase B.1 — capture app shell. Section 1 is a verifiable placeholder only, so
// we can confirm the scaffold runs and is reachable on a phone over the tunnel
// before any camera code exists (Section 2). See ../PHASE_B1_CAPTURE_PROTOTYPE.md.
const PLAYER_ID = import.meta.env.VITE_REF_PLAYER_ID;

export default function App() {
  return (
    <main className="screen">
      <div className="card">
        <h1>Capture</h1>
        <p className="muted">Reference photo capture — coming up.</p>
        <p className="target">
          {PLAYER_ID
            ? <>Uploads will target player <b>#{PLAYER_ID}</b></>
            : <>No <code>VITE_REF_PLAYER_ID</code> set — copy <code>.env.example</code> to <code>.env</code> before Section 5.</>}
        </p>
      </div>
    </main>
  );
}
