/* Pipeline progress display.
 *
 * Hidden when status != 'running'. Shows the current stage label and a
 * progress bar (determinate when total > 0, indeterminate striped when 0).
 */
const STAGE_LABELS = {
  starting:    'Starting…',
  detecting:   'Detecting faces',
  clustering:  'Clustering by player',
  coach_check: 'Checking for coaches',
  labeling:    'Reading copyright tags',
  classifying: 'Reading expressions',
  sorting:     'Assigning roles',
  error:       'Pipeline error',
};

export default function PipelineProgress({ session, compact = false }) {
  if (!session || session.status !== 'running') return null;

  const stage = session.progress_stage || 'starting';
  const current = session.progress_current || 0;
  const total = session.progress_total || 0;
  const label = STAGE_LABELS[stage] || stage;
  const determinate = total > 0;
  const pct = determinate ? Math.round((current / total) * 100) : 0;

  return (
    <div className={`pipeline-progress ${compact ? 'compact' : ''}`}>
      <div className="pipeline-progress-row">
        <span className="stage-label">{label}</span>
        {determinate && (
          <span className="stage-count">{current} / {total} ({pct}%)</span>
        )}
      </div>
      <div className={`progress-bar ${determinate ? '' : 'indeterminate'}`}>
        {determinate
          ? <div className="progress-fill" style={{ width: `${pct}%` }} />
          : <div className="progress-fill indeterminate" />}
      </div>
    </div>
  );
}
