# Lumi — OpenAI Build Week Submission

**Category:** Apps for Your Life

**Pitch:** Lumi turns one screen you choose into a safe, structured brief, then proposes only the follow-up actions you explicitly approve.

## Starting point

Before Build Week, Lumi was a working Electron MVP with a floating Windows companion, one-time user-selected screen capture, a deterministic mock path, OpenAI Realtime voice/image context, and confirmation-gated reminders, approved-folder search, file opening, and URL opening.

## What was built during Build Week

- Prepared the product for public submission under the Lumi name.
- Added a distinct GPT-5.6 screen-review workflow to the core capture experience.
- Kept the permanent OpenAI key in Electron main and added a narrow typed IPC route that accepts only a retained capture ID.
- Added strict JSON Schema output plus application-side validation for summaries, dates, safe links, risks, and next actions.
- Added a visible renderer confirmation before the GPT-5.6 request, while preserving main-process validation and the existing confirmation model for every follow-up action.
- Added focused tests, public documentation, safe environment defaults, an MIT license, and repository hygiene checks.

## GPT-5.6's exact role

When a user has selected and captured a screen or window, Lumi presents **Review this capture with GPT-5.6**. Choosing it explicitly authorizes one read-only Responses API request from Electron main.

The request sends only the retained in-memory capture to `gpt-5.6-terra` with `reasoning.effort: low`, `store: false`, and strict JSON Schema output. GPT-5.6 produces a concise summary plus visible dates, `http`/`https` links, risks, and suggested next actions. Lumi validates that closed schema before showing anything. The model has no desktop-control tool, cannot access files, and cannot carry out a reminder, file open, URL open, or message send.

The configured `gpt-5.6-terra` model and strict structured-output Responses API request were verified live on 20 July 2026.

## Codex's exact role

Codex was used during Build Week to inspect the Electron security boundary, implement and test the GPT-5.6 main-process integration, verify the structured Responses API request, improve the README and environment guidance, audit repository hygiene, and run the submission validation commands.

## Safety and privacy model

- Lumi never records or continuously monitors the desktop.
- Capture is user initiated, and the GPT-5.6 review has its own visible confirmation.
- `OPENAI_API_KEY` stays in Electron main. Renderer code uses only the fixed `window.lifeLens` bridge.
- Captures used for GPT-5.6 review are memory bounded and not persisted by Lumi.
- State-changing and external actions require renderer confirmation, payload validation, and a main-process confirmation step before execution.
- File search is restricted to user-approved roots; arbitrary local file access is outside scope.

## Judge-ready path

1. Configure a local `OPENAI_API_KEY` and run `npm.cmd run dev`.
2. Open an interview email or calendar-like event page containing a visible date, a preparation request, and an `https` link.
3. Open Lumi, choose that window, and capture it once.
4. Confirm **Review this capture with GPT-5.6**.
5. Verify the structured brief names supported dates, links, risks, and suggested next actions.
6. Optionally confirm a reminder or an extracted link; reject one first to verify that rejection does nothing.

The repeatable manual checklist is [DEMO-CHECKLIST.md](DEMO-CHECKLIST.md).

## Current limitations

- Windows release artifacts are not code signed.
- The five-consecutive-run live hero-scenario record is still pending.
- GPT-5.6 review is intentionally limited to an explicitly confirmed screen capture; arbitrary document reading and autonomous computer use are not supported.
- The deterministic no-key path demonstrates the interaction and confirmation flow but does not call GPT-5.6.

## Submission placeholders

- **Final YouTube demo:** Add the final URL before submission.
- **Primary Codex `/feedback` session ID:** Add the session ID before submission.
