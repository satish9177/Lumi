# LifeLens

LifeLens is a Windows desktop companion for short, user-requested screen understanding. It is being built as an Electron, React, TypeScript, and Vite MVP.

## Current state

The MVP now implements the bounded LifeLens workflow:

- a transparent, draggable, always-on-top companion opens a compact panel;
- deterministic mock voice works without credentials, and the live path uses OpenAI Realtime over WebRTC;
- the user can choose a screen or window, capture it once, and receive an explanation with date, link, and next-action signals;
- the model can propose each bounded tool: `create_reminder`, `search_documents`, `open_file`, `open_url`, and `save_context`;
- the renderer presents a confirmation card and the main process presents a second native confirmation before every state-changing or external action;
- document search is bounded to explicitly approved folders, and a file can be opened only if it came from a previous approved search; and
- reminders retain the source summary and signals used to explain the later notification.

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

The build produces an NSIS installer and an unpacked executable under `release/0.1.0/`. This repository does not include a signing certificate. On the current Windows host, Smart App Control correctly blocks the unsigned executable, so a trusted Authenticode signing step is required before claiming a packaged-launch pass. Do not disable or bypass Windows protection for this app.

## Safety model

- Capture is explicitly user initiated.
- The renderer has a narrow typed bridge, not Node or generic Electron IPC.
- The renderer presents every state-changing or external tool action for visible confirmation.
- The main process validates a confirmed proposal and asks for a native confirmation again before it acts.
- Document search is restricted to folders selected by the user; whole-drive scanning is not in scope.
- Reminder source context is stored so a later notification can explain why it exists.

## Remaining acceptance work

This repository does **not** yet claim the definition of done. The remaining acceptance work is:

- sign the Windows release with a trusted certificate and repeat the packaged-app smoke test;
- exercise a live Realtime session with an operator-provided API key and microphone permission;
- complete the full interview-email hero scenario, including approved-folder search and selected file/URL opening, five consecutive times; and
- record those five runs in the demo checklist.

LifeLens deliberately does not support continuous screen monitoring, arbitrary computer control, message sending, payments, credentials, OTPs, mobile clients, cloud sync, or whole-drive scanning.

See [the goal](/C:/Users/satis/Projects/Lumi/docs/GOAL.md), [architecture decisions](/C:/Users/satis/Projects/Lumi/docs/DECISIONS.md), [current status](/C:/Users/satis/Projects/Lumi/docs/STATUS.md), and [the demo checklist](/C:/Users/satis/Projects/Lumi/docs/DEMO-CHECKLIST.md) for the delivery plan and verification record.
