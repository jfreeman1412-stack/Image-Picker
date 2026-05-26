// Phase B.3 §6 — the lossless home for sync-time rejections. Offline, the server
// can't reject a capture in front of the volunteer, so failures surface here:
//  • failed_quality — the server's quality gate (blurry / multiple faces / no
//    face). Shows the server's own message.
//  • failed_gone — the target player or shoot no longer exists on the server.
// Nothing is auto-deleted. Re-shoot replaces the failed item with a fresh
// capture; Discard is the only non-200 deletion (confirmation-gated).
// Items are the CURRENT shoot's failed queue records (passed from App).
// See ../PHASE_B3_OFFLINE_CAPTURE.md.
import { useState } from 'react';

export default function NeedsAttention({ job, items, onReshoot, onDiscard, onBack }) {
  const [confirmId, setConfirmId] = useState(null);

  return (
    <main className="screen roster-screen">
      <header className="roster-header">
        <button className="link-back" onClick={onBack}>← Roster</button>
        <div className="roster-titles">
          <h1 className="roster-title">Needs attention</h1>
          <span className="roster-count">{job?.name}</span>
        </div>
      </header>

      <div className="roster-body">
        {items.length === 0 ? (
          <p className="muted roster-pad">Nothing needs attention for this shoot.</p>
        ) : (
          <ul className="roster-list">
            {items.map((item) => {
              const gone = item.status === 'failed_gone';
              return (
                <li key={item.id} className="attention-item">
                  <div className="attention-main">
                    <span className="roster-name">{item.playerName}</span>
                    <span className="roster-team">{item.team}</span>
                  </div>
                  <p className="attention-reason">
                    {gone ? 'Target removed on the server: ' : 'Rejected on sync: '}
                    {item.lastError}
                  </p>
                  {confirmId === item.id ? (
                    <div className="controls-inline">
                      <span className="muted small">Discard this photo?</span>
                      <button className="btn ghost" onClick={() => setConfirmId(null)}>Cancel</button>
                      <button className="btn danger" onClick={() => onDiscard(item)}>Discard</button>
                    </div>
                  ) : (
                    <div className="controls-inline">
                      {!gone && (
                        <button className="btn" onClick={() => onReshoot(item)}>Re-shoot</button>
                      )}
                      <button className="btn ghost" onClick={() => setConfirmId(item.id)}>Discard</button>
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </main>
  );
}
