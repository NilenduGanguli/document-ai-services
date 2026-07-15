/**
 * Colour-coded badges for the domain enums (sensitivity, gate decision,
 * verification status, job status). Tone mapping lives here so the same value
 * always reads the same colour on every page, in both themes.
 */
import type { ReactNode } from 'react';
import type { GateDecision, JobStatus, SensitivityBucket, VerificationStatus } from '../lib/types';

export type Tone = 'neutral' | 'info' | 'green' | 'amber' | 'red' | 'gold';

interface BadgeProps {
  tone?: Tone;
  children: ReactNode;
  dot?: boolean;
  large?: boolean;
  title?: string;
}

export function Badge({
  tone = 'neutral',
  children,
  dot = false,
  large = false,
  title,
}: BadgeProps): JSX.Element {
  return (
    <span className={`badge ${tone}${large ? ' badge-lg' : ''}`} title={title}>
      {dot && <span className="badge-dot" />}
      {children}
    </span>
  );
}

/** LOW → neutral, MEDIUM → info, HIGH → amber, CRITICAL → red. */
export function sensitivityTone(value: string | null | undefined): Tone {
  switch ((value ?? '').toUpperCase()) {
    case 'CRITICAL':
      return 'red';
    case 'HIGH':
      return 'amber';
    case 'MEDIUM':
      return 'info';
    case 'LOW':
      return 'neutral';
    default:
      return 'neutral';
  }
}

export function SensitivityBadge({
  value,
}: {
  value: SensitivityBucket | string | null | undefined;
}): JSX.Element {
  if (!value) return <span className="cell-muted">—</span>;
  return (
    <Badge tone={sensitivityTone(value)} dot title={`Sensitivity bucket: ${value}`}>
      {String(value).toUpperCase()}
    </Badge>
  );
}

/**
 * DETERMINISTIC_ONLY is the *safest* outcome (nothing left the perimeter), so
 * it reads green; SEND_TO_LLM means raw content was sent, so it reads amber.
 */
export function gateTone(value: string | null | undefined): Tone {
  switch (value) {
    case 'DETERMINISTIC_ONLY':
      return 'green';
    case 'REDACT_THEN_SEND':
      return 'info';
    case 'SEND_TO_LLM':
      return 'amber';
    default:
      return 'neutral';
  }
}

const GATE_LABEL: Record<string, string> = {
  DETERMINISTIC_ONLY: 'Deterministic only',
  REDACT_THEN_SEND: 'Redact → send',
  SEND_TO_LLM: 'Send to LLM',
};

const GATE_TITLE: Record<string, string> = {
  DETERMINISTIC_ONLY: 'No content left the perimeter — extracted deterministically.',
  REDACT_THEN_SEND: 'Sensitive spans were redacted before the LLM call.',
  SEND_TO_LLM: 'Content was sent to the LLM for extraction.',
};

export function GateBadge({
  value,
}: {
  value: GateDecision | string | null | undefined;
}): JSX.Element {
  if (!value) return <span className="cell-muted">—</span>;
  const key = String(value);
  return (
    <Badge tone={gateTone(key)} title={GATE_TITLE[key] ?? `Gate decision: ${key}`}>
      {GATE_LABEL[key] ?? key}
    </Badge>
  );
}

export function verificationTone(value: string | null | undefined): Tone {
  switch (value) {
    case 'gov_verified':
      return 'green';
    case 'checksum_verified':
      return 'green';
    case 'llm_unverified':
      return 'amber';
    case 'unverified':
      return 'neutral';
    default:
      return 'neutral';
  }
}

const VERIF_LABEL: Record<string, string> = {
  gov_verified: 'Gov verified',
  checksum_verified: 'Checksum verified',
  llm_unverified: 'LLM unverified',
  unverified: 'Unverified',
};

const VERIF_TITLE: Record<string, string> = {
  gov_verified: 'Confirmed against a government endpoint.',
  checksum_verified: 'The identifier passed its checksum.',
  llm_unverified: 'Extracted by the LLM; not independently verified.',
  unverified: 'No verification was performed.',
};

export function VerificationBadge({
  value,
}: {
  value: VerificationStatus | string | null | undefined;
}): JSX.Element {
  if (!value) return <span className="cell-muted">—</span>;
  const key = String(value);
  return (
    <Badge tone={verificationTone(key)} title={VERIF_TITLE[key] ?? key}>
      {VERIF_LABEL[key] ?? key}
    </Badge>
  );
}

export function jobTone(status: JobStatus | string | null | undefined): Tone {
  switch (status) {
    case 'succeeded':
      return 'green';
    case 'failed':
      return 'red';
    case 'running':
      return 'info';
    case 'queued':
      return 'neutral';
    default:
      return 'neutral';
  }
}

export function JobStatusBadge({
  status,
}: {
  status: JobStatus | string | null | undefined;
}): JSX.Element {
  if (!status) return <span className="cell-muted">—</span>;
  return (
    <Badge tone={jobTone(status)} dot>
      {String(status)}
    </Badge>
  );
}

/** Confidence bar: ≥0.8 green, ≥0.5 amber, below that red. */
export function ConfidenceBar({ value }: { value: number | null | undefined }): JSX.Element {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return <span className="cell-muted">—</span>;
  }
  const pct = Math.max(0, Math.min(1, value)) * 100;
  const tone = value >= 0.8 ? 'green' : value >= 0.5 ? 'amber' : 'red';
  return (
    <div
      className="confbar"
      role="meter"
      aria-valuenow={Math.round(pct)}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-label={`Confidence ${Math.round(pct)}%`}
    >
      <span className="confbar-track">
        <span className={`confbar-fill ${tone}`} style={{ width: `${pct}%` }} />
      </span>
      <span className="confbar-val">{Math.round(pct)}%</span>
    </div>
  );
}
