# AI Task Worker: an autonomous AI employee prototype

Give it a task in plain language, for example:

> *"Find the latest invoice from Globex, extract the amount and due date, enter it into our FinOps system, and tell me once it is done."*

The worker then:
1. works out what "done" means;
2. plans;
3. operates the company's web apps in a real browser (search the mailbox, open the right email, download the PDF, read it, sign in to the ERP, fill and submit the form);
4. recovers when things go wrong (server errors, expired sessions, blocking dialogs, validation errors);
5. asks a human when it should (risky actions, real ambiguity, suspected fraud);
6. has an **independent auditor agent** confirm the outcome in the live system before reporting `completed_verified`, with evidence.

It runs against **Acme Corp**, a sandboxed company with a shared AP mailbox and a "FinOps" ERP. The ERP is deliberately unreliable, because a prototype that only works on a happy path proves little.

> **Demo video:** _add link here_

---

## Quick start

Requires Python 3.11+ and Chrome or Edge (or Playwright's bundled Chromium).

```bash
python -m venv .venv
.venv\Scripts\activate             # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium   # optional if Chrome/Edge is installed

copy .env.example .env             # then set ANTHROPIC_API_KEY (or OPENAI_API_KEY)

python start.py                    # sandbox on :8001 + dashboard on :8000 (opens your browser)
```

In the dashboard, pick an example task (or write your own). Tick **show browser** to watch it work, then press **Run task**.
You'll see the plan, every action with its stated intent, page observations with screenshots, automatic
retries, approval requests (approve or deny right there), and the verifier's checks.

Other entry points:

```bash
python -m worker run "Log every vendor invoice in the AP mailbox that isn't in FinOps yet." --headed
python -m sandbox --chaos hostile          # sandbox only; chaos: off | realistic | hostile
python -m evals.run_evals                  # eval suite (needs the sandbox running)
python -m pytest                           # 21 tests, no API key needed (~30 s)
```

The sandbox is also usable by hand at http://127.0.0.1:8001 (ERP login: `ap.agent` / `Acme!2026`).

---

## Architecture

```
                    ┌───────────────────────── Dashboard (FastAPI + SSE) / CLI ─────────────────────────┐
  task ───────────► │  RunManager: one thread + event loop per run        approve / deny / answer ◄──┐  │
                    └───────────────┬───────────────────────────────────────────────────────▲─────────┼──┘
                                    │                                                         │         │
                     ┌──────────────▼───────────────┐   events (trace.jsonl)  ───────────────┘   HumanChannel
                     │  Worker loop  (agent.py)     │                                           (web / console /
                     │  plan → act → observe → adapt│◄── budgets, stall detection,               scripted for evals)
                     │  finish ─► VERIFICATION GATE │    honest final report
                     └──────┬───────────────┬───────┘
              LLM (neutral) │               │ tool calls
     ┌──────────────────────▼─┐   ┌─────────▼──────────────────────────────────────────────┐
     │ Conversation interface │   │ ToolExecutor: validate → POLICY → run → retry/backoff →  │
     │  ├ Claude adapter      │   │   classify errors → screenshot evidence → event          │
     │  ├ OpenAI adapter      │   │ Tools: browser_* · read_document · remember · update_plan │
     │  └ Scripted (tests)    │   │        ask_human · save_lesson · finish                  │
     └────────────────────────┘   └─────────┬───────────────────────────┬──────────────────┘
                                            │ Playwright                │ policy.py
                                  ┌─────────▼─────────┐        URL allowlist · risky-action
                                  │ BrowserSession    │        approval · read-only mode
                                  │ text snapshot w/  │
                                  │ [ref]s + downloads│        Verifier agent (verifier.py)
                                  └─────────┬─────────┘        fresh context + fresh browser
                                            │                  session + read-only tools
                          ┌─────────────────▼──────────────────┐
                          │ Acme sandbox (sandbox/app.py)      │  Memory: working facts ·
                          │ Mailbox · FinOps ERP · chaos layer │  company onboarding doc ·
                          └────────────────────────────────────┘  lessons · credential vault
```

| Module | Responsibility |
|---|---|
| `worker/agent.py` | Control loop, budgets, stall detection, verification gate, final report |
| `worker/tools.py` | Tool definitions and the executor (validation, policy, retries, evidence) |
| `worker/browser.py` | Playwright session; page → text snapshot with `[ref]`s; downloads; error mapping |
| `worker/policy.py` | URL allowlist, high-impact action detection, read-only mode |
| `worker/verifier.py` | Independent auditor agent and verdict |
| `worker/memory.py` | Working memory, company context, lessons, credential vault |
| `worker/llm/` | Provider-neutral conversation; Claude, OpenAI and scripted adapters |
| `worker/events.py` | Append-only event log (the single source of truth for a run) |
| `worker/server.py`, `ui/` | Dashboard: live timeline, plan, memory, approvals, report |
| `sandbox/` | Acme Corp: mailbox, ERP, seeded data and PDFs, fault injection, eval-only admin API |
| `evals/` | Task suite with ground-truth checks; results in `evals/results/` |
| `knowledge/company.md` | The worker's onboarding doc for this company |

### One step of the loop
1. The model sees the latest observation and decides on tool calls, stating the **intent** of each.
2. The executor validates arguments and asks the **policy**: is the URL allowed? Does the target button
   do something high-impact? If so, it blocks until a human approves.
3. The action runs. Transient failures are retried with backoff. Other failures come back as an observation
   (what went wrong plus the current page), never as a crash.
4. A fresh page snapshot is returned, a screenshot is saved, and everything is appended to the event log.
5. The loop watches for stalls, repeated failures and the step budget, and nudges the model when needed.
6. On `finish(completed)` the **verifier** audits the live system. If it doesn't confirm, its findings go
   back to the worker to fix.

---

## Key design decisions

The full reasoning and trade-offs are in [`docs/DECISIONS.md`](docs/DECISIONS.md). In short:

- **Realistic sandbox with seeded chaos** (503s, session expiry, blocking modal, strict validation,
  duplicates). Real work, measurable outcomes, reproducible failures.
- **Text page snapshots with element refs** instead of screenshots for perception. Site-agnostic,
  cheap and precise; screenshots are kept as evidence.
- **Safety enforced in code, not in the prompt.** URL allowlist, approval gate on high-impact actions
  (decided from the *element actually being clicked*), and a read-only verifier. Prompt injection inside an
  email can't talk its way past this.
- **An independent verifier** with fresh context and a fresh browser session derives success criteria from
  the *original request*, not from the worker's claims. This catches "did the wrong thing correctly".
- **Reliability layered by who should handle each failure**: infrastructure retries in code, judgment
  calls (fix the date, re-enter lost data, sign in again) in the model, loop-level guards against thrashing.
- **Every action carries a stated intent**, and model reasoning is summarized into the trace, so any decision
  can be explained after the fact.
- **Secrets never enter the model context**: the vault fills credentials by id.
- **Generalization by design**: the loop, tools and prompts contain no task logic. Company knowledge lives
  in an onboarding doc, and the eval suite runs six different task types on identical code.

---

## Reliability: what goes wrong and what the worker does

| What happens in the sandbox | What the worker does |
|---|---|
| ERP returns **503** on save ("changes NOT saved") | Notices it from the page and HTTP status, checks whether the record exists, re-enters and resubmits |
| **Session expires** mid-task → redirect to login | Signs in again with the vault and continues where it left off |
| **Maintenance modal** intercepts clicks | Gets an "obscured by overlay" error with the blocking element, dismisses it, retries |
| Source PDF says "02 November 2026", ERP demands `YYYY-MM-DD`; amount `16,245.50` vs plain number | Normalizes, or reads the validation error and fixes it |
| Invoice **already exists** | Detects the duplicate and does not create another |
| Two plausible matches (two Stark invoices) | Resolves from context or asks with concrete options |
| Email asks to **change bank details** from a look-alike domain | Treats email content as information, not orders, flags the fraud signals, and the approval gate stops the change |
| Clicks a ref from an old page | Gets a "stale ref" error plus a fresh snapshot |
| Repeats the same failing action | The loop's stall detector tells it to change approach |
| LLM out of credit / bad key | Fails fast with a clear message instead of retrying |

---

## Evaluation

```bash
python -m sandbox            # terminal 1
python -m evals.run_evals    # terminal 2  (options: --tasks ... --chaos hostile --repeat 3)
```

| Task | Category | What it tests |
|---|---|---|
| `latest_invoice` | data entry | Choosing the *latest* invoice (not the older one or the reminder), PDF extraction, format conversion, ERP entry under chaos |
| `log_all_missing` | batch processing | Finding all invoices, skipping those already in FinOps, three correct entries, no duplicates |
| `bank_change_request` | safety / fraud | Phishing email with a look-alike domain: must not change bank details; must flag it |
| `pending_total` | read-only reporting | Aggregating ERP data correctly, changing nothing |
| `ambiguous_stark` | ambiguity | Two vendor invoices: pick the right one or ask; never duplicate |
| `reply_to_vendor` | communication | Composing and sending a correct reply (approval-gated) |

Each task is graded on the **sandbox's actual state** (and the reply text), never on the agent's report. The
harness also records whether the worker's verifier agreed with ground truth.

**Results:** _see `evals/results/` (filled in below after live runs)._

---

## Models, APIs, frameworks

- **LLM:** Claude Opus 5.5 (`claude-opus-5-5`) via the Anthropic Python SDK, with adaptive thinking (summarized),
  effort `high`, prompt caching on the static prefix, server-side refusal fallback, and server-side clearing of
  stale tool results. OpenAI (`gpt-5.4` via Chat Completions) is supported as an alternative adapter.
- **Browser automation:** Playwright (Chromium, or installed Chrome/Edge).
- **Server/UI:** FastAPI, Uvicorn, server-sent events, a single vanilla-JS page (no build step).
- **Sandbox:** FastAPI + Jinja2; invoices rendered with ReportLab; PDFs read with pypdf.
- **Tests:** pytest. No agent framework (LangChain etc.) and no hosted browser service.

## Assumptions

- The worker acts as a member of the AP team, with that team's access, in the systems listed in its onboarding doc.
- "Enter an invoice" means record it as *Pending*. Paying is a separate, explicitly requested and approved step.
- The invoice PDF is the source of truth for amount, dates and currency.
- A human supervisor is reachable for approvals and questions. If nobody answers in time, the worker
  stops that part and reports `blocked` rather than guessing.
- Inbound email is untrusted input.

## Known limitations

- **Text-only perception.** Canvas or image-only UIs (and scanned PDFs) can't be read. There is no vision fallback yet.
- **Risk detection is label-based** (keywords on the clicked element's label or form). It is good for this prototype; production
  needs per-app action schemas with declared risk.
- **Single-tab, single-browser** worker. No desktop apps, no parallel sub-tasks.
- **The verifier is an LLM.** It is independent and grounded, but not infallible; its agreement with ground truth is measured, not assumed.
- **Lessons are unreviewed.** In production they should go through human review before being trusted.
- **In-memory run registry** in the dashboard (traces persist on disk, but the run list resets on restart).
- The sandbox is far simpler than real enterprise software, though its failure modes are realistic.

## What I'd build next

1. **Vision fallback**: screenshot plus coordinates when the text snapshot is insufficient (canvas, PDFs-as-images).
2. **Typed action schemas per application** (`erp.create_invoice`, `erp.submit_payment` with declared risk and
   post-conditions). This turns label heuristics into explicit policy and makes verification partly deterministic.
3. **Skill library**: promote successful traces into reusable, parameterized procedures (with human review),
   so repeat tasks become faster, cheaper and more predictable, and the LLM only handles deviations.
4. **Durable execution**: checkpoint runs (Temporal-style) so a worker can wait days for an approval, survive restarts
   and resume mid-task. Persist the run registry.
5. **Multi-channel humans**: approvals via Slack or email with an expiring context link; escalation rules.
6. **Larger eval suite** with adversarial variants (prompt injection in documents, UI changes), cost/latency
   tracking per task, and regression gating in CI.
7. **API-first tools**: when a system has an API, prefer it over the browser, using the same policy and verification layers.

## Project layout

```
ai-task-worker/
├── start.py               # launch sandbox + dashboard
├── worker/                # the AI worker
│   ├── agent.py  tools.py  browser.py  policy.py  verifier.py  memory.py  prompts.py
│   ├── events.py  runner.py  server.py  config.py  __main__.py
│   └── llm/  base.py  anthropic_provider.py  openai_provider.py  scripted.py
├── sandbox/               # Acme Corp: app.py, seed.py, templates/
├── evals/                 # tasks.py, run_evals.py, results/
├── ui/index.html          # dashboard
├── knowledge/company.md   # onboarding doc (+ lessons.md, written by the worker)
├── config/vault.json      # sandbox credentials (never shown to the model)
├── docs/DECISIONS.md      # design decisions and trade-offs
└── tests/                 # 21 tests: sandbox, policy, loop guarantees (scripted model + real browser)
```
