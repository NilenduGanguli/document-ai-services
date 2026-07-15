/**
 * Live pipeline visualisation: renders the canonical stage list
 * (ocr → gate → extract → subtree → arep → merge → done) and fills each step in
 * from the job's `events` array.
 *
 * The backend's event `status` vocabulary is normalised defensively here so a
 * new status string never renders a step as silently blank.
 */
import { detailPairs, formatTs } from '../lib/format';
import { STAGES, type Job, type JobEvent } from '../lib/types';
import { Badge } from './Badge';
import { IconAlert, IconCheck, IconSpinner } from './Icons';

type StepState = 'pending' | 'active' | 'done' | 'failed';

const DONE_WORDS = new Set(['ok', 'done', 'succeeded', 'success', 'complete', 'completed', 'finished']);
const FAIL_WORDS = new Set(['error', 'failed', 'failure', 'fatal']);
const ACTIVE_WORDS = new Set(['running', 'started', 'start', 'in_progress', 'active', 'pending']);

function normalise(status: string | undefined | null): StepState | null {
  const s = (status ?? '').toLowerCase();
  if (DONE_WORDS.has(s)) return 'done';
  if (FAIL_WORDS.has(s)) return 'failed';
  if (ACTIVE_WORDS.has(s)) return 'active';
  return null;
}

/** Human blurb per stage, shown before the job reports anything for it. */
const STAGE_HINT: Record<string, string> = {
  ocr: 'Extract text + layout from the raw bytes',
  gate: 'Classify, bucket sensitivity, decide the LLM route',
  extract: 'Pull canonical facts (deterministic + LLM)',
  subtree: 'Build the document knowledge subtree',
  arep: 'Generate accessibility representations',
  merge: 'Merge facts into the client-level view',
  done: 'Pipeline complete',
};

interface StageView {
  stage: string;
  state: StepState;
  event: JobEvent | null;
}

/**
 * Fold the job's events into a per-stage view.
 *
 * Any stage the server reports that is not in the canonical list is appended,
 * so an evolving pipeline still renders completely.
 */
export function buildStageViews(job: Job | null): StageView[] {
  const events = job?.events ?? [];
  const order: string[] = [...STAGES];
  for (const e of events) {
    if (e.stage && !order.includes(String(e.stage))) order.push(String(e.stage));
  }

  // Last event wins per stage.
  const latest = new Map<string, JobEvent>();
  for (const e of events) {
    if (e.stage) latest.set(String(e.stage), e);
  }

  const reached = new Set(latest.keys());
  const jobFailed = job?.status === 'failed';
  const jobDone = job?.status === 'succeeded';

  return order.map((stage) => {
    const event = latest.get(stage) ?? null;
    let state: StepState = 'pending';

    if (event) {
      state = normalise(event.status) ?? 'done';
    }

    // The job's own cursor marks the in-flight stage.
    if (job?.stage === stage && job.status === 'running' && state !== 'failed') {
      state = 'active';
    }
    // A failed job marks its current stage as the failure point, even if the
    // last event for it never arrived.
    if (jobFailed && job?.stage === stage) state = 'failed';
    // A succeeded job implies every reached stage completed.
    if (jobDone && (reached.has(stage) || stage === 'done')) {
      if (state !== 'failed') state = 'done';
    }
    return { stage, state, event };
  });
}

function Marker({ state, index }: { state: StepState; index: number }): JSX.Element {
  if (state === 'done') return <IconCheck size={13} />;
  if (state === 'failed') return <IconAlert size={13} />;
  if (state === 'active') return <IconSpinner size={13} />;
  return <>{index + 1}</>;
}

export function StageStepper({ job }: { job: Job | null }): JSX.Element {
  const views = buildStageViews(job);

  return (
    <ol className="stepper" aria-label="Pipeline stages">
      {views.map((v, i) => (
        <li key={v.stage} className={`step ${v.state}`}>
          <span className="step-marker" aria-hidden>
            <Marker state={v.state} index={i} />
          </span>
          <div className="step-body">
            <div className="step-name">
              {v.stage}
              {v.state === 'active' && <Badge tone="info">running</Badge>}
              {v.state === 'failed' && <Badge tone="red">failed</Badge>}
              {v.event?.ts && <span className="step-time">{formatTs(v.event.ts)}</span>}
              <span className="sr-only">{`status: ${v.state}`}</span>
            </div>

            {v.event?.detail ? (
              <div className="step-detail">
                {detailPairs(v.event.detail).map(([k, val]) => (
                  <span className="kv" key={k}>
                    {k} <b title={val}>{val}</b>
                  </span>
                ))}
              </div>
            ) : (
              v.state === 'pending' && (
                <div className="cell-muted" style={{ fontSize: 12 }}>
                  {STAGE_HINT[v.stage] ?? ''}
                </div>
              )
            )}
          </div>
        </li>
      ))}
    </ol>
  );
}
