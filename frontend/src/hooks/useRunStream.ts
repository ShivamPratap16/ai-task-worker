import { useEffect, useReducer } from "react";
import { api } from "../lib/api";
import { initialRun, runReducer } from "../lib/runState";
import type { RunEvent } from "../lib/types";

/** Live state of one run, replayed from its event log and then followed over Server-Sent Events. */
export function useRunStream(runId: string | null, knownTask = "") {
  const [state, dispatch] = useReducer(runReducer, initialRun(knownTask));

  useEffect(() => {
    dispatch({ type: "reset", task: knownTask });
    if (!runId) return;
    const source = new EventSource(api.eventsUrl(runId));
    source.onmessage = (m) => {
      try { dispatch({ type: "event", event: JSON.parse(m.data) as RunEvent }); } catch (e) { console.error(e); }
    };
    source.addEventListener("end", () => { source.close(); dispatch({ type: "end" }); });
    return () => source.close();
    // knownTask only seeds the header before the first event; it must not restart the stream.
  }, [runId]);

  return state;
}
