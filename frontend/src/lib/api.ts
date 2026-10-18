import type {
  ApplyAuditActionResult,
  AuditReportResponse,
  BiometricLog,
  ChatIngestResponse,
  ClinicalNode,
  SupplementLog,
  DailyJournal,
  FullGraph,
  LabResult,
  MorningBriefing,
  ProposedAction,
  RegimenItem,
  RegimenProposalItem,
  RegimenRemovalItem,
} from './types';

const BASE_URL = process.env.NEXT_PUBLIC_API_URL ?? '';

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  });

  if (!res.ok) {
    const body = await res.text().catch(() => res.statusText);
    throw new Error(`API ${res.status}: ${body}`);
  }

  return res.json() as Promise<T>;
}

// ── Chat ─────────────────────────────────────────────────────────────────────

export async function postChatIngest(text: string, loggingEnabled = true): Promise<ChatIngestResponse> {
  return apiFetch<ChatIngestResponse>('/api/v1/chat/ingest', {
    method: 'POST',
    body: JSON.stringify({ text, logging_enabled: loggingEnabled }),
  });
}

// ── History reads ─────────────────────────────────────────────────────────────

export async function fetchLabResults(limit = 500): Promise<LabResult[]> {
  return apiFetch<LabResult[]>(
    `/api/v1/get/history?entry_type=lab_result&limit=${limit}`,
  );
}

export async function fetchBiometrics(limit = 500): Promise<BiometricLog[]> {
  return apiFetch<BiometricLog[]>(
    `/api/v1/get/history?entry_type=biometric&limit=${limit}`,
  );
}

export async function fetchSupplements(limit = 100): Promise<SupplementLog[]> {
  return apiFetch<SupplementLog[]>(
    `/api/v1/get/history?entry_type=supplement&limit=${limit}`,
  );
}

export async function fetchJournals(limit = 50): Promise<DailyJournal[]> {
  return apiFetch<DailyJournal[]>(
    `/api/v1/get/history?entry_type=daily_journal&limit=${limit}`,
  );
}

// ── Entry update ─────────────────────────────────────────────────────────────

export async function updateEntry(
  entryType: 'biometric' | 'supplement' | 'lab' | 'journal',
  id: string,
  fieldName: string,
  newValue: string,
): Promise<void> {
  await apiFetch(`/api/v1/entry/${entryType}/${id}`, {
    method: 'PATCH',
    body: JSON.stringify({ field_name: fieldName, new_value: newValue }),
  });
}

// ── Entry deletion ────────────────────────────────────────────────────────────

export async function deleteEntry(
  entryType: 'biometric' | 'supplement' | 'lab' | 'journal',
  id: string,
): Promise<void> {
  const res = await fetch(`${BASE_URL}/api/v1/entry/${entryType}/${id}`, {
    method: 'DELETE',
  });
  if (!res.ok) {
    const body = await res.text().catch(() => res.statusText);
    throw new Error(`Delete failed ${res.status}: ${body}`);
  }
}

/**
 * Call POST /api/v1/chat/rewind and return the raw SSE Response.
 * The caller is responsible for reading the stream, using the same event protocol as
 * /api/v1/chat/ingest (status / chunk / done / error events).
 */
export async function rewindChat(
  sessionId: string,
  messageId: string,
  newContent: string,
): Promise<Response> {
  const res = await fetch(`${BASE_URL}/api/v1/chat/rewind`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      session_id: sessionId,
      message_id: messageId,
      new_content: newContent,
    }),
  });
  if (!res.ok) {
    const body = await res.text().catch(() => res.statusText);
    throw new Error(`Rewind failed ${res.status}: ${body}`);
  }
  return res;
}

export async function clearChatHistory(): Promise<void> {
  const res = await fetch(`${BASE_URL}/api/v1/chat/history`, { method: 'DELETE' });
  if (!res.ok) {
    const body = await res.text().catch(() => res.statusText);
    throw new Error(`Clear failed ${res.status}: ${body}`);
  }
}

// ── Regimen ───────────────────────────────────────────────────────────────────

export async function fetchRegimen(): Promise<RegimenItem[]> {
  return apiFetch<RegimenItem[]>('/api/v1/regimen');
}

export async function deleteRegimenItem(id: string): Promise<void> {
  const res = await fetch(`${BASE_URL}/api/v1/regimen/${id}`, { method: 'DELETE' });
  if (!res.ok && res.status !== 204) {
    const body = await res.text().catch(() => res.statusText);
    throw new Error(`Delete failed ${res.status}: ${body}`);
  }
}

// ── HITL Regimen Proposal ─────────────────────────────────────────────────────

export async function saveRegimenBatch(items: RegimenProposalItem[]): Promise<void> {
  await apiFetch<void>('/api/v1/regimen/batch', {
    method: 'POST',
    body: JSON.stringify({ items }),
  });
}

export async function deleteRegimenByName(
  supplement_name: string,
  time_of_day?: string,
): Promise<void> {
  const payload: Record<string, string> = { supplement_name };
  if (time_of_day) payload.time_of_day = time_of_day;
  const res = await fetch(`${BASE_URL}/api/v1/regimen/by-name`, {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!res.ok && res.status !== 204) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(`Delete by name failed ${res.status}: ${text}`);
  }
}

// ── Knowledge Graph / Morning Briefing ───────────────────────────────────────

export async function fetchMorningBriefing(): Promise<MorningBriefing> {
  return apiFetch<MorningBriefing>('/api/v1/insights/morning-briefing');
}

export async function fetchFullGraph(): Promise<FullGraph> {
  return apiFetch<FullGraph>('/api/v1/insights/full-graph');
}

export interface SynthesisStatus {
  cooldown_remaining_seconds: number;
  cooldown_total_seconds: number;
  last_synthesis_date: string | null;
}

export interface ForceSynthesisResult {
  /** undefined when the server returned 429 (cooldown active) */
  success?: boolean;
  synthesis_date?: string;
  nodes_created?: number;
  nodes_updated?: number;
  edges_created?: number;
  /** seconds in the cooldown window after a successful run */
  cooldown_seconds?: number;
  /** seconds remaining, set on 429 responses */
  cooldown_remaining_seconds?: number;
}

export async function fetchSynthesisStatus(): Promise<SynthesisStatus> {
  return apiFetch<SynthesisStatus>('/api/v1/insights/synthesis-status');
}

export async function postForceSynthesis(): Promise<ForceSynthesisResult> {
  const res = await fetch(`${BASE_URL}/api/v1/insights/force-synthesis`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
  });
  const body = await res.json().catch(() => ({}));
  if (res.status === 429) {
    // Cooldown active: return remaining seconds so the button can display a countdown
    return { cooldown_remaining_seconds: body.cooldown_remaining_seconds ?? SYNTHESIS_COOLDOWN_FALLBACK };
  }
  if (!res.ok) {
    throw new Error(body.detail ?? `Synthesis failed: ${res.status}`);
  }
  return body as ForceSynthesisResult;
}

// Matches SYNTHESIS_COOLDOWN_SECONDS on the server, used as a fallback if the
// server doesn't return the value (e.g. unexpected error shape).
const SYNTHESIS_COOLDOWN_FALLBACK = 600;

export async function fetchActiveTracking(): Promise<ClinicalNode[]> {
  return apiFetch<ClinicalNode[]>('/api/v1/insights/active-tracking');
}

export async function fetchDailyInquiries(): Promise<{
  questions: string[];
  generated_at: string | null;
}> {
  return apiFetch<{ questions: string[]; generated_at: string | null }>(
    '/api/v1/insights/inquiries',
  );
}

// ── Red Team Audit ────────────────────────────────────────────────────────────

export async function fetchAuditReport(): Promise<AuditReportResponse> {
  return apiFetch<AuditReportResponse>('/api/v1/insights/audit-report');
}

export interface RunAuditResult {
  success?: boolean;
  challenges_found?: number;
  nodes_audited?: number;
  summary?: string;
  /** seconds remaining, set on 429 (cooldown active) responses */
  cooldown_remaining_seconds?: number;
}

export async function postRunAudit(): Promise<RunAuditResult> {
  const res = await fetch(`${BASE_URL}/api/v1/insights/run-audit`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
  });
  const body = await res.json().catch(() => ({}));
  if (res.status === 429) {
    return { cooldown_remaining_seconds: body.cooldown_remaining_seconds ?? AUDIT_COOLDOWN_FALLBACK };
  }
  if (!res.ok) {
    throw new Error(body.detail ?? `Audit failed: ${res.status}`);
  }
  return body as RunAuditResult;
}

export async function postApplyAuditAction(
  concept_name: string,
  action: ProposedAction,
): Promise<ApplyAuditActionResult> {
  return apiFetch<ApplyAuditActionResult>('/api/v1/insights/apply-audit-action', {
    method: 'POST',
    body: JSON.stringify({ concept_name, action }),
  });
}

// Matches AUDIT_COOLDOWN_SECONDS on the server (1 hour).
const AUDIT_COOLDOWN_FALLBACK = 3600;

// ── Cognition loop feedback ─────────────────────────────────────────────────────

export async function sendCognitionFeedback(
  rating: 'up' | 'down' | 'snooze',
  signature?: string | null,
): Promise<{ ok: boolean; effective_speak_bar?: number }> {
  return apiFetch('/api/v1/cognition/feedback', {
    method: 'POST',
    body: JSON.stringify({ rating, signature: signature ?? null }),
  });
}

// ── Health ────────────────────────────────────────────────────────────────────

export async function fetchHealth(): Promise<{ status: string }> {
  return apiFetch<{ status: string }>('/api/v1/health');
}

// ── Garmin sync ───────────────────────────────────────────────────────────────

export interface GarminSyncResult {
  dates_synced: number;
  synced: Array<{ metric_name: string; value: number; unit: string; date: string }>;
  skipped: string[];
  errors: string[];
}

export async function syncGarmin(opts: { force?: boolean; days?: number } = {}): Promise<GarminSyncResult> {
  const params = new URLSearchParams();
  if (opts.force) params.set('force', 'true');
  if (opts.days)  params.set('days',  String(opts.days));
  const qs = params.toString();
  return apiFetch<GarminSyncResult>(`/api/v1/sync/garmin${qs ? `?${qs}` : ''}`, { method: 'POST' });
}
