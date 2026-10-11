# Design decisions

Each entry: what was decided, why, and what it costs. Ordered roughly by how much they shape the system.

---

### D1. Build a realistic sandbox company instead of targeting real websites
**Decision.** A self-contained "Acme Corp" intranet (`sandbox/`): a shared AP mailbox with PDF invoices and a
FinOps ERP with login, forms, vendors and payments.
**Why.** The brief rules out real credentials and third-party systems, and asks for a narrow prototype that
*genuinely works* over broad mocked functionality. A sandbox lets the agent do real work (real HTTP, real
forms, real PDFs, real state changes) while letting us (a) inject failures deliberately and (b) grade
outcomes against ground truth.
**Cost.** The apps are simpler than real SaaS. Mitigated by making them *misbehave like real internal tools*
(D2) and by keeping the agent completely site-agnostic (D4), so nothing in the worker knows about Acme.

### D2. Deliberate, seeded fault injection ("chaos profiles")
**Decision.** The ERP injects transient 503s on form posts, expires sessions after N requests, shows a
blocking maintenance modal, rejects human-formatted dates/amounts, and rejects duplicates. Profiles: `off`,
`realistic`, `hostile`; every profile is seeded, so a failure sequence replays identically.
**Why.** "Reliability" is untestable on a happy path. These are the failure modes that break real browser
agents: lost form data after a server error, being logged out mid-task, overlays intercepting clicks, and
format mismatches between source documents and target systems. Seeding makes evals comparable across agent
versions.
**Cost.** None at runtime; the profile is a sandbox setting.

### D3. A custom agent loop over provider-native tool calling (no agent framework)
**Decision.** A small explicit loop (`worker/agent.py`) using each provider's tool-calling API directly.
**Why.** The parts that matter for this brief (budgets, stall detection, a hard verification gate, approval
enforcement, honest reporting) are control-flow decisions that need to be explicit and debuggable.
Frameworks hide exactly those. About 200 lines of loop code are easy to explain, modify live, and test.
**Cost.** No ecosystem integrations for free. Tools are plain async functions, so adding one is cheap.

### D4. Perception through a text "page snapshot" with element refs, not screenshots
**Decision.** After every action the agent receives a compact, document-ordered text rendering of the page:
headings, text, table rows, alerts and dialogs, and every visible interactive element tagged `[ref]` with its
label, current value and options. Actions address elements by ref.
**Why.** It is site-agnostic (works on any HTML app), cheap in tokens, deterministic, and it removes the
largest error source of pixel-based agents (mis-clicks). Refs are re-issued on every snapshot and every action
returns a fresh snapshot, so the model always acts on what it just saw. Stale refs fail loudly with a clear
message. Screenshots are still captured after every action as *human-facing evidence* and for debugging.
**Cost.** Canvas-heavy or visual-only UIs aren't readable as text. A vision fallback is the next step
(see README, "What's next").

### D5. Safety is enforced in code (policy layer), not requested in the prompt
**Decision.** `worker/policy.py` runs inside the tool executor:
- URL allowlist: the agent cannot leave the company's systems or reach admin endpoints.
- High-impact actions (payment, bank details, send, delete, approve) are detected from the *target
  element's label / enclosing form* and pause for human approval. A denial is final.
- Read-only mode for the verifier: only navigation, search and sign-in are allowed.
**Why.** A model can be persuaded (for example by a prompt injection inside an email) to do anything; a
code-level gate cannot. Classifying on observable facts (the button the click lands on) rather than on what
the model *says* it's doing removes the model from the trust path.
**Cost.** Keyword classification over labels is coarse. In production this becomes per-application action
schemas with explicit risk levels (see "What's next").

### D6. Independent verifier agent, plus a hard gate on "completed"
**Decision.** When the worker calls `finish(status="completed")`, a *separate* agent audits the outcome:
fresh context (it never saw the worker's reasoning), a fresh browser session (it observes the system of record,
not the worker's last screen), read-only tools, and success criteria derived from the *original request*.
A negative verdict sends the specific findings back to the worker to fix (up to 2 rounds). The final
outcome distinguishes `completed_verified` from `completed_unverified`.
**Why.** Self-assessment by the acting agent is biased toward "done". The most damaging failure is doing
the wrong thing correctly (for example entering the *older* Globex invoice perfectly). Only a check against
the request catches that. Verification is a first-class evaluation criterion in the brief.
**Cost.** Roughly 20–40% more tokens per completed task. Worth it: an unverified "done" is what makes
humans re-check everything, which defeats the purpose.
**Measured.** The eval harness compares the verifier's verdict with ground truth, so we know how much to
trust it (README, "Evaluation").

### D7. Reliability layered by who should handle each failure
| Failure | Handled by | Why there |
|---|---|---|
| Network blips, timeouts | Tool executor: automatic retry with exponential backoff | Model needn't spend a turn on it |
| LLM rate limits / 5xx | SDK retries, plus loop-level retry | Transient |
| Out of credit, bad key, invalid request | Fail fast (`LLMError.fatal`) | Retrying can't help. Found the hard way: the first live run retried an out-of-credit error for 2 minutes |
| Validation error, 503 page, session expiry, modal | The model, via an informative observation | Needs judgment: reformat the date, re-enter lost data, sign in again |
| Stale element ref / obscured click | Executor returns a clear error plus a fresh snapshot | Model re-orients immediately |
| Repeating the same failing action | Loop stall detector injects an operator note | Models perseverate; a nudge breaks the loop |
| Many consecutive failures / budget nearly spent | Operator note: wrap up honestly | Prefer an honest "blocked" over thrashing |
| Unexpected tool exception | Converted to an observation, never a crash | One bad tool call must not lose the run |
| Anything else | Run-level guard always writes a report | The operator always gets an outcome |

### D8. Every tool call states its intent
**Decision.** Browser actions require an `intent` argument ("why I'm doing this"), shown in the dashboard and trace.
**Why.** The interview asks "explain why the agent made particular decisions". With intents plus
summarized model reasoning, every action in the trace carries its own justification.
**Cost.** A few tokens per action.

### D9. Secrets never enter the model's context
**Decision.** Credentials live in a vault (`config/vault.json`). The model sees only credential *ids* and
calls `browser_fill_credential(ref, "finops", "password")`. Password fields render as `********` in snapshots.
**Why.** Model context is logged, cached and can be exfiltrated by prompt injection. An AI employee needs
access without knowing the secret.

### D10. Memory at three time scales
- **Working memory** (`remember`): facts found during the run (amounts, record ids), reported with the result.
- **Company context** (`knowledge/company.md`): an operator-written *onboarding doc* covering systems, URLs and
  house rules. Swapping this file is how the same worker joins another company; no code changes.
- **Lessons** (`save_lesson`): short procedural learnings persisted across runs ("FinOps needs ISO dates"),
  capped and de-duplicated. Disabled in evals so every run starts equal.
**Why.** This is the "hyper-personalized around each company's workflows" part of the brief, in its
simplest working form.

### D11. Provider-neutral LLM layer; each adapter owns its native transcript
**Decision.** The loop talks to a 3-method `Conversation` interface. The Claude adapter appends raw response
blocks (so thinking blocks are echoed back unchanged and history stays append-only, as Claude Opus 5.5
requires) and uses server-side clearing of old tool results to bound context. The OpenAI adapter elides
stale page snapshots client-side.
**Why.** Context management is provider-specific and getting it wrong is subtle; isolating it in adapters
keeps the loop clean. It also made the system testable with a scripted provider (D13), and let it switch
providers when one account ran out of credit.
**Default model.** Claude Opus 5.5 with adaptive thinking (summarized, so reasoning is visible) at `high`
effort. Long-horizon tool use rewards the strongest model, and summarized thinking makes decisions explainable.

### D12. The event log is the single source of truth
**Decision.** Everything (thoughts, actions, observations, retries, approvals, verification) is an event
appended to `runs/<id>/trace.jsonl`. The dashboard, CLI and report are projections of it. The SSE stream
tails the log from a cursor, so reconnecting replays history.
**Why.** Debuggability ("debug an issue" is part of the interview) and auditability for an AI employee.
Any run can be inspected after the fact, step by step, with screenshots.

### D13. Test loop guarantees with a scripted model against the real browser and sandbox
**Decision.** `worker/llm/scripted.py` drives the real loop, tools, Playwright browser and sandbox through
exact scenarios. Tests assert *guarantees*: a denied approval never reaches the system; a false
"completed" is rejected and fed back; stalls are detected; the agent cannot leave allowed origins; the
verifier cannot write.
**Why.** LLM behaviour varies; these guarantees must not. They run in about 30 s with no API cost.
Behavioural quality (does the model do the task?) is measured separately by the eval suite (D14).

### D14. Evals grade the system of record, not the agent's report
**Decision.** `evals/` has 6 task types (data entry, batch processing, fraud handling, read-only
reporting, ambiguity, outbound communication) with ground-truth checks against the sandbox's state, run
under chaos. The worker code and prompt are identical across tasks.
**Why.** This measures the brief's "Generalization" directly, and tells us how often self-verification
can be trusted (verifier vs ground-truth agreement).

### D15. Per-run thread with its own event loop
**Decision.** Each run executes on its own thread with its own asyncio loop (Proactor on Windows).
**Why.** Playwright needs subprocess support, which the selector loop some ASGI servers install on Windows
lacks. Isolation also means one hung run can't stall the dashboard or other runs.

### D16. Pluggable providers, including a free tier, behind a registry
**Decision.** `config.PROVIDERS` lists each provider in one entry: key variable, default model, endpoint,
default rate limit. Claude has its own adapter. Gemini, OpenAI and any OpenAI-compatible server (Ollama,
OpenRouter, Groq) share one Chat Completions adapter that differs only by base URL. Auto-detection order is
Anthropic > Gemini > OpenAI. `python -m worker check` validates a setup with a single request.
**Why.** Cost matters to the builder (the prototype must be testable for free) and to the business (the model is a
swappable dependency, not the product). The free Gemini tier makes development possible at $0. Supporting any
OpenAI-compatible endpoint covers local and self-hosted models with no new code.
**Provider quirks are handled in the adapter:**
- Free tiers have a per-minute request cap, so a process-wide sliding-window limiter paces requests.
- A 429 can mean two different things: "daily quota or credit gone" is fatal and stops the run immediately,
  while "per-minute limit" waits and retries.
- Some compatible backends reject `additionalProperties` in tool schemas, so schemas are sanitized.
- Some backends omit tool-call ids, so ids are generated.

**Cost.** Free-tier models are weaker than Opus 5.5, and the daily quota limits how many eval runs fit in a
day. The README states which model produced each result.

### D17. Cache the whole conversation on Claude, not just the system prompt
**Decision.** In addition to the breakpoint on the static system prompt, requests set top-level
`cache_control` so the growing history (every earlier page snapshot) is read from cache on the next step.
**Why.** An agent re-sends its entire history every step. Without history caching, a 25-step task paid
full input price for roughly 700k tokens. With it, each step pays full price only for the newest turn,
roughly 4–5x cheaper per task. I found this while estimating test costs, before any money was spent.
