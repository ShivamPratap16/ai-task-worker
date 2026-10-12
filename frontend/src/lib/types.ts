// Shapes of the Python API (worker/server.py) and of the run event log (worker/events.py).

export type Outcome =
  | "completed_verified" | "completed_unverified" | "blocked" | "failed" | "error" | "cancelled";

export type StepStatus = "pending" | "in_progress" | "done" | "skipped" | "failed";

export interface PlanStep { step: string; status: StepStatus }

export interface PendingRequest { id: string; kind: string; question: string; options?: string[] }

export interface RunSummary {
  run_id: string;
  task: string;
  status: string;
  created: number;
  outcome: Outcome | null;
  pending: PendingRequest[];
  steps?: number | null;
  duration_s?: number | null;
  tokens?: number | null;
  model?: string | null;
}

export interface QuotaModel { model: string; requests: number; daily_limit?: number; exhausted: boolean; resets_in?: string }
export interface Quota { models: QuotaModel[]; active: string | null }
export interface Examples { tasks: string[]; provider: string }

export interface Check { check: string; passed: boolean; evidence?: string }
export interface Verification { achieved: boolean; confidence: string; checks: Check[]; issues: string[] }
export interface Usage { input_tokens: number; output_tokens: number; cache_read_tokens: number }

export interface Finished {
  outcome: Outcome;
  summary?: string;
  claims?: string[];
  evidence?: string[];
  verification?: Verification | null;
  steps?: number;
  duration_s?: number;
}

/** One entry of runs/<id>/trace.jsonl, as streamed over SSE. */
export interface RunEvent { seq: number; ts: number; type: string; data: EventData }

/** Union of the payload fields the dashboard reads; every event carries a subset. */
export interface EventData {
  actor?: "worker" | "verifier";
  task?: string; provider?: string; model?: string;
  text?: string; reasoning?: string;
  call_id?: string; tool?: string; args?: Record<string, unknown>; intent?: string;
  ok?: boolean; error_kind?: string; content?: string; screenshot?: string | null; url?: string | null;
  attempt?: number; error?: string; delay_s?: number; reason?: string;
  request_id?: string; action?: string; approved?: boolean; note?: string; responder?: string;
  kind?: string; question?: string; options?: string[]; steps?: PlanStep[]; answer?: string;
  key?: string; value?: string; lesson?: string;
  round?: number; achieved?: boolean; confidence?: string; checks?: Check[]; issues?: string[];
  input_tokens?: number; output_tokens?: number; cache_read_tokens?: number;
  where?: string; message?: string;
  from?: string; to?: string;
  outcome?: Outcome; summary?: string; claims?: string[]; evidence?: string[];
  verification?: Verification | null; duration_s?: number;
}
