---
title: AI Task Worker
emoji: 🤖
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
short_description: Autonomous AI worker operating a sandboxed company's web apps
---

# AI Task Worker: live demo

An autonomous AI worker that takes a plain-language task and completes it in a real browser against
**Acme Corp**, a sandboxed company (mailbox + FinOps ERP with injected failures), then has an independent
verifier agent confirm the outcome.

Source code, architecture and design decisions: https://github.com/ShivamPratap16/ai-task-worker

**Using the demo**
- Sign in with the demo password you were given (it protects the free-tier LLM quota).
- Pick an example task and press **Run task**. Approve or deny the worker's requests as they appear.
- The browser runs headless here; every step's screenshot is in the run's timeline.
- The Space sleeps when idle; the first load can take about a minute.
- The free LLM tier allows only a few full tasks per day; if the quota is used up, the run says so.
