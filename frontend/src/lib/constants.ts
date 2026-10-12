import type { IconName } from "../components/Icon";
import type { Outcome } from "./types";

export type Tone = "ok" | "warn" | "err" | "none";

export const OUTCOME: Record<Outcome, { tone: Tone; label: string; icon: IconName }> = {
  completed_verified: { tone: "ok", label: "Completed & verified", icon: "shield" },
  completed_unverified: { tone: "warn", label: "Completed, not verified", icon: "alert" },
  blocked: { tone: "warn", label: "Blocked — needs a human", icon: "flag" },
  failed: { tone: "err", label: "Failed", icon: "x" },
  error: { tone: "err", label: "Error", icon: "x" },
  cancelled: { tone: "none", label: "Cancelled", icon: "stop" },
};

export const outcomeOf = (o: string) =>
  OUTCOME[o as Outcome] ?? { tone: "none" as Tone, label: o, icon: "flag" as IconName };

export type ApprovalMode = "risky" | "always" | "never";

export const POLICY: Record<ApprovalMode, { icon: IconName; label: string; hint: string }> = {
  risky: { icon: "shield", label: "Ask for risky", hint: "Pauses for your approval on payments, bank-detail changes and other risky actions." },
  always: { icon: "lock", label: "Ask for all", hint: "Pauses for approval before every change it makes. Safest; slowest." },
  never: { icon: "zap", label: "Autonomous", hint: "Never pauses for approval. Policy guardrails still block forbidden actions." },
};

const TOOL_LABEL: Record<string, [IconName, string]> = {
  browser_navigate: ["globe", "Navigate"], browser_click: ["pointer", "Click"], browser_type: ["keyboard", "Type"],
  browser_select: ["list", "Select"], browser_fill_credential: ["key", "Sign in"], browser_back: ["back", "Go back"],
  browser_snapshot: ["eye", "Read page"], read_document: ["file", "Read document"], list_downloads: ["folder", "List downloads"],
  ask_human: ["msg", "Ask a human"], finish: ["flag", "Finish"], save_lesson: ["book", "Save lesson"],
};

export function toolMeta(tool: string): [IconName, string] {
  return TOOL_LABEL[tool] ?? [
    /verdict/.test(tool) ? "shield" : "zap",
    tool.replace(/^browser_/, "").replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase()),
  ];
}

/** Tools whose effects are shown elsewhere (plan panel, memory panel, question card). */
export const QUIET_TOOLS = new Set(["update_plan", "remember", "ask_human"]);

export function exampleIcon(task: string): IconName {
  if (/bank/i.test(task)) return "bank";
  if (/how much|owe|total/i.test(task)) return "calc";
  if (/reply|email/i.test(task)) return "mail";
  if (/every|all/i.test(task)) return "list";
  return "file";
}
