import { useCallback, useEffect, useState } from "react";
import { api } from "../lib/api";
import type { Quota, RunSummary } from "../lib/types";

export function useStored(key: string, initial: string): [string, (v: string) => void] {
  const [value, setValue] = useState(() => {
    try { return localStorage.getItem(key) ?? initial; } catch { return initial; }
  });
  const set = useCallback((v: string) => {
    setValue(v);
    try { localStorage.setItem(key, v); } catch { /* storage unavailable: keep in memory only */ }
  }, [key]);
  return [value, set];
}

/** Current run id from the URL hash (#<run_id>), so runs are linkable and survive refresh. */
export function useHashRoute(): [string | null, (id: string | null) => void] {
  const read = () => decodeURIComponent(location.hash.slice(1)) || null;
  const [route, setRoute] = useState(read);
  useEffect(() => {
    const onHash = () => setRoute(read());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  const go = useCallback((id: string | null) => {
    history.replaceState(null, "", id ? `#${encodeURIComponent(id)}` : location.pathname);
    setRoute(id);
  }, []);
  return [route, go];
}

/** Run list and quota, refreshed every few seconds (cheap local calls). */
export function usePolling() {
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [quota, setQuota] = useState<Quota | null>(null);
  const refresh = useCallback(async () => {
    try { setRuns(await api.runs()); } catch { /* server restarting: keep the last list */ }
    try { setQuota(await api.quota()); } catch { /* optional endpoint */ }
  }, []);
  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 3000);
    return () => clearInterval(t);
  }, [refresh]);
  return { runs, quota, refresh };
}

export function useTheme(): [boolean, () => void] {
  const [stored, setStored] = useStored("theme", "");
  const systemDark = typeof matchMedia !== "undefined" && matchMedia("(prefers-color-scheme: dark)").matches;
  const dark = stored ? stored === "dark" : systemDark;
  useEffect(() => {
    if (stored) document.documentElement.dataset.theme = stored;
    else delete document.documentElement.dataset.theme;
  }, [stored]);
  return [dark, () => setStored(dark ? "light" : "dark")];
}

/** Ticks once a second while `active`, for live elapsed-time counters. */
export function useNow(active: boolean) {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(t);
  }, [active]);
  return now;
}
