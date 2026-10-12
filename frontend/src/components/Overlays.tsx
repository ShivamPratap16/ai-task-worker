import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { Icon } from "./Icon";

interface Toast { id: number; text: string; tone: "ok" | "err" }
interface Overlays { toast: (text: string, tone?: "ok" | "err") => void; zoom: (src: string) => void }

const Ctx = createContext<Overlays>({ toast: () => {}, zoom: () => {} });
export const useOverlays = () => useContext(Ctx);

/** Toast notifications and the full-screen screenshot viewer, available anywhere via useOverlays(). */
export function OverlayProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [zoomed, setZoomed] = useState<string | null>(null);

  const toast = useCallback((text: string, tone: "ok" | "err" = "err") => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t, { id, text, tone }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 5000);
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setZoomed(null); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  return (
    <Ctx.Provider value={{ toast, zoom: setZoomed }}>
      {children}
      <div id="lightbox" className={zoomed ? "on" : ""} role="dialog" aria-label="Screenshot" onClick={() => setZoomed(null)}>
        {zoomed && <img src={zoomed} alt="Screenshot" />}
      </div>
      <div className="toasts" role="status" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} className={`toast ${t.tone}`}><Icon name={t.tone === "ok" ? "check" : "alert"} /><div>{t.text}</div></div>
        ))}
      </div>
    </Ctx.Provider>
  );
}
