# LifeLens

LifeLens is a Windows desktop companion for short, user-requested screen understanding. It is being built as an Electron, React, TypeScript, and Vite MVP.

## Current state

The first deterministic vertical slice is working:

- a transparent, draggable, always-on-top companion opens a compact panel;
- mock voice connects when no key is configured;
- the user can capture the primary display;
- the app shows an interview-email explanation with a date, link, and next action; and
- a visible confirmation saves a mock reminder together with its source context.

The production Realtime path uses WebRTC. The permanent `OPENAI_API_KEY` remains in the Electron main process, which exchanges it for a short-lived renderer credential; it is never bundled into renderer code.

## Run locally

Install dependencies, then start the development app:

```powershell
npm.cmd install
npm.cmd run dev
```

Run the verification commands:

```powershell
npm.cmd run typecheck
npm.cmd test
npm.cmd run build
```

To exercise live OpenAI Realtime rather than the deterministic mock transport, provide `OPENAI_API_KEY` in the process environment before launching the app. Do not put the key in source code, renderer configuration, or a committed `.env` file.

```powershell
$env:OPENAI_API_KEY = "your-key-for-this-shell-only"
npm.cmd run dev
```

Create a Windows installer with:

```powershell
npm.cmd run package
```

## Safety model

- Capture is explicitly user initiated.
- The renderer has a narrow typed bridge, not Node or generic Electron IPC.
- The renderer presents every state-changing or external tool action for visible confirmation.
- The main process validates a confirmed proposal again before it acts.
- Future document search is restricted to folders selected by the user; whole-drive scanning is not in scope.
- Reminder source context is stored so a later notification can explain why it exists.

## Not supported yet

This repository does **not** yet claim a finished MVP or a packaged-app smoke test. The following hero-scenario work is still pending:

- selected-window capture and a capture picker;
- live Realtime smoke testing with a configured API key;
- approved-folder selection, document search, and selected-result opening;
- confirmed `open_url` and `save_context` execution;
- full live tool-call handling for all five supported proposal types; and
- five consecutive complete hero-scenario passes.

LifeLens deliberately does not support continuous screen monitoring, arbitrary computer control, message sending, payments, credentials, OTPs, mobile clients, cloud sync, or whole-drive scanning.

See [the goal](/C:/Users/satis/Projects/Lumi/docs/GOAL.md), [architecture decisions](/C:/Users/satis/Projects/Lumi/docs/DECISIONS.md), [current status](/C:/Users/satis/Projects/Lumi/docs/STATUS.md), and [the demo checklist](/C:/Users/satis/Projects/Lumi/docs/DEMO-CHECKLIST.md) for the delivery plan and verification record.
