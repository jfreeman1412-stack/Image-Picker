/* Sticky action bar for plowing through a job team-by-team.
 *
 * The dominant action is "Mark reviewed & next" — flips the reviewed flag and
 * navigates to the next unreviewed team in the job, or back to the job page
 * if none remain. Secondary buttons let the user skip without marking, or
 * step backwards.
 */
export default function ReviewActionBar({
  reviewed,
  hint,            // boolean: show the "press R" hint
  variant,         // 'sticky' | 'inline' (top mirror)
  onMarkAndNext,
  onSkipNext,
  onPrev,
  prevDisabled,
  nextDisabled,
  busy,
}) {
  const label = reviewed ? 'Unmark reviewed & next' : 'Mark reviewed & next';
  return (
    <div className={`review-actionbar ${variant}`}>
      <button
        className="ghost"
        onClick={onPrev}
        disabled={prevDisabled || busy}
      >← Previous team</button>

      <div className="spacer" />

      <button
        className="ghost"
        onClick={onSkipNext}
        disabled={nextDisabled || busy}
      >Skip to next →</button>

      <button
        className={`primary ${reviewed ? 'undo' : ''}`}
        onClick={onMarkAndNext}
        disabled={busy}
      >
        {reviewed ? '↺' : '✓'} {label}
        {hint && <span className="hint">press R</span>}
      </button>
    </div>
  );
}
