# Lumi

**A private-by-default desktop companion that turns one user-chosen screen into a safe, actionable brief.**

Lumi is a Windows Electron app for the moment when an email, form, meeting page, or task board is on screen and you need to understand what matters next. It can review one screen or window that you choose, surface dates and links, and propose bounded follow-up actions that you must explicitly approve.

## The problem

Important details are often scattered across one busy screen: deadlines, preparation links, and a next step hidden in an email or portal. Lumi makes that context useful without continuously watching the desktop or taking control of the computer.

## What works today

- A lightweight floating Windows companion with a deterministic no-key demo mode.
- User-initiated capture of one selected screen or application window.
- Live voice and text conversation through OpenAI Realtime when an API key is configured; selected-photo analysis remains separately confirmed.
- An explicit GPT-5.6 review of that one captured image, returning a validated brief of visible dates, links, risks, and suggested next actions.
- Confirmed reminders, approved-folder search, opening a returned local file, and opening an extracted `http` or `https` link.
- Optional Telegram connection and confirmed attachment sending when configured locally.

Lumi does **not** claim Gmail or Calendar access, document-content understanding, continuous monitoring, autonomous desktop control, whole-drive scanning, message sending without confirmation, or cloud sync.

## Recommended judge workflow

1. Start Lumi and open the floating companion.
2. Keep an interview email or event page with a date, preparation request, and link visible.
3. Select that window, then capture it once.
4. Choose **Review this capture with GPT-5.6** in the visible confirmation card.
5. Inspect the structured review: summary, dates, links, risks, and suggested next actions.
6. If useful, explicitly confirm a reminder or an allowed link. Nothing executes until confirmation.

The complete manual path is in [docs/DEMO-CHECKLIST.md](docs/DEMO-CHECKLIST.md).

## Architecture

| Layer | Responsibility |
| --- | --- |
| `src/renderer` | React UI, screen-source selection, visible confirmations, and WebRTC lifecycle. It uses only `window.lifeLens`. |
| `src/preload` | The narrow, typed `contextBridge` API. It exposes no generic IPC or Node APIs. |
| `src/main` | Screen capture, OpenAI requests, native confirmations, local storage, approved-folder controls, and IPC validation. |
| `src/shared` | Typed contracts and runtime payload validators shared across the boundary. |

## OpenAI model usage

| Model | Exact role |
| --- | --- |
| `gpt-realtime-2.1-mini` | Default live Realtime model for Lumi's WebRTC voice, text, selected-photo context, and bounded tool-proposal flow. Screen reviews provide it only validated text. `LIFELENS_REALTIME_MODEL` may select a supported alternative. |
| `gpt-4o-mini-transcribe` | Input transcription for the Realtime voice flow. |
| `gpt-5.6-terra` | Default model for the separate, explicitly confirmed screen-review request through the Responses API. |

### GPT-5.6 in Lumi

GPT-5.6 is genuinely used at runtime; it is not a renamed Realtime model. After a user captures a chosen screen or window, Lumi shows a second renderer confirmation. Only after the user chooses **Review this capture with GPT-5.6** does Electron main send that retained in-memory image to the Responses API.

Captures are initiated and previewed locally. A selected capture is sent to GPT-5.6 Terra only after the user explicitly requests review. The Realtime voice session receives the validated textual review rather than the screenshot.

The request uses `gpt-5.6-terra`, `reasoning.effort: low`, `store: false`, and strict JSON Schema output. Main validates the closed response schema before returning the summary, date list, safe `http`/`https` links, risks, and suggested next actions to the UI. GPT-5.6 has no desktop-control tools and cannot execute actions. The model and strict-output call were verified against the live Responses API on 20 July 2026.

## Build Week and Codex

Lumi entered Build Week with its safe Electron MVP: a user-selected capture flow, Realtime conversation, and confirmation-gated local actions. During Build Week, the project was polished as Lumi and gained the explicit GPT-5.6 structured screen-review path, hardened repository hygiene, judge documentation, and focused validation.

Codex helped inspect the security boundary, implement and test the GPT-5.6 main-process integration, verify the live structured-output request, improve submission documentation, and run the repository checks. See [docs/BUILD-WEEK.md](docs/BUILD-WEEK.md) for the submission narrative.

## Privacy and safety

- Screen capture is always user initiated; Lumi never continuously watches the desktop.
- The permanent `OPENAI_API_KEY` remains in Electron main. The renderer receives only a short-lived Realtime credential and cannot access Node or Electron APIs.
- GPT-5.6 receives an image only after the separate, visible confirmation; the retained capture is bounded in memory and never persisted by Lumi.
- Every external or state-changing follow-up is confirmation gated in the renderer and revalidated in main before execution.
- Document search stays inside folders the user explicitly approves. Opening files is limited to results from those searches.

More detail: [docs/DECISIONS.md](docs/DECISIONS.md) and [docs/GOAL.md](docs/GOAL.md).

## Windows installation

**Requirements:** Windows 10 or 11 (x64), a current Node.js LTS release, and npm. A configured OpenAI API key is required for live Realtime and GPT-5.6 review; without one, Lumi runs its deterministic mock flow.

```powershell
git clone https://github.com/satish9177/Lumi.git
Set-Location Lumi
npm.cmd install
```

Set secrets only in your local process environment or another ignored local configuration. `.env.example` documents every supported variable and contains no credentials.

```powershell
$env:OPENAI_API_KEY = "your-key-for-this-shell-only"
$env:LUMI_REASONING_MODEL = "gpt-5.6-terra"
npm.cmd run dev
```

Optional Telegram variables are documented in [.env.example](.env.example). Do not commit `.env`, session files, logs, or local databases.

## Development commands

```powershell
npm.cmd run typecheck
npm.cmd test
npm.cmd run build
npm.cmd run package
```

`npm.cmd run package` creates an unsigned Windows installer under `release/0.1.0/`. A trusted Authenticode signing step is still required before distributing a production build; do not disable Windows security controls to run an unsigned package.

## Judge testing

Use the [demo checklist](docs/DEMO-CHECKLIST.md) and confirm these boundaries while testing:

- no key: mock interaction remains usable and sends nothing to OpenAI;
- live key: a user-selected capture requires the GPT-5.6 review confirmation before the Responses API call;
- a rejected reminder, file open, URL open, or Telegram send performs no action; and
- folder search returns only results under a user-approved folder.

## Known limitations

- The packaged Windows build is not code signed yet.
- A full live hero-scenario acceptance run still needs to be recorded five consecutive times.
- GPT-5.6 review is intentionally image-only and only covers a capture the user explicitly approves; it does not read arbitrary local documents.

## Repository status

This is a public Build Week repository. The app is an MVP with a focused, working hero flow and documented release prerequisites. Current delivery notes are in [docs/STATUS.md](docs/STATUS.md).

## License

Lumi is released under the [MIT License](LICENSE).
Third-party model and dependency notices are in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
