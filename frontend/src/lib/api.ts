import type { Examples, Quota, RunSummary } from "./types";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, init);
  if (!r.ok) {
    let message = await r.text();
    try { message = JSON.parse(message).detail ?? message; } catch { /* plain-text error */ }
    throw new Error(message || r.statusText);
  }
  return r.json() as Promise<T>;
}

const post = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body) });

const run = (id: string) => `/api/runs/${encodeURIComponent(id)}`;

export const api = {
  examples: () => request<Examples>("/api/examples"),
  runs: () => request<RunSummary[]>("/api/runs"),
  quota: () => request<Quota>("/api/quota"),
  start: (body: { task: string; approval_mode: string; headless: boolean; plan_review: boolean }) =>
    post<RunSummary>("/api/runs", body),
  respond: (id: string, body: { request_id: string; approved: boolean | null; answer: string }) =>
    post<{ ok: boolean }>(`${run(id)}/respond`, body),
  message: (id: string, text: string) => post<{ ok: boolean }>(`${run(id)}/message`, { text }),
  cancel: (id: string) => post<{ ok: boolean }>(`${run(id)}/cancel`),
  eventsUrl: (id: string) => `${run(id)}/events`,
  reportUrl: (id: string) => `${run(id)}/report`,
  shotUrl: (id: string, name: string) => `${run(id)}/files/screenshots/${encodeURIComponent(name)}`,
};
