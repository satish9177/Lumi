# Lumi

**A private-by-default desktop companion that turns one screen you choose into a safe, actionable brief.**

Built for **OpenAI Build Week 2026** with Codex and GPT-5.6.
Category: *Apps for Your Life* · Supported platform: **Windows 10/11 x64**

- **Demo video:** [Watch on YouTube](https://www.youtube.com/watch?v=znW03ult8w8)
- **Repository:** https://github.com/satish9177/Lumi

---

## The problem

The thing you need to act on is usually already on your screen — a hospital appointment with preparation instructions, a bank message with a warning, or a form with a deadline. The details are visible but scattered, and the next step is buried in the middle of a paragraph.

The obvious fix is an assistant that watches your desktop and clicks things for you. That trade is bad: continuous screen recording and autonomous computer control are a lot of risk for a small convenience.

Lumi takes the narrow version of that bet. You choose one window. You capture it once. You explicitly approve a review. You get a structured brief, and every follow-up action asks first.

## What Lumi does

**One approved screen, reviewed by GPT-5.6**
- A floating Windows companion that stays out of the way.
- You pick a single screen or application window and capture it once — Lumi never watches continuously.
- A separate, visible confirmation (**Review this capture with GPT-5.6**) is required before the image leaves your machine.
- GPT-5.6 returns a validated brief: a summary, visible dates, safe `http`/`https` links, risks, and suggested next actions.
- The same approved-capture path can produce a scam-warning risk assessment. It does not authenticate a sender or prove that a message is safe.

**Live conversation**
- Voice and text through the OpenAI Realtime API over WebRTC.
- The Realtime session receives the *validated text* of a screen review, never the screenshot itself.

**Actions that always ask first**
- Creating a reminder, opening a returned file or extracted link, sending one selected photo to the Realtime session for analysis, saving context, and optional Telegram sends.
- Every state-changing or external action is confirmation-gated in the UI and revalidated in the main process before it runs. A reminder proposal can be cancelled with no reminder created; saved-reminder deletion is not implemented in this MVP.
- File search is read-only, user initiated, and restricted to folders the user explicitly approves. Opening a result is a separate confirmed action.

**On-device indexing and search**
- Semantic photo search powered by CLIP ViT-B/32 running locally on ONNX Runtime (CPU).
- Opt-in local OCR (Tesseract) for words and numbers in photos or screenshots, plus opt-in visible-face counting.
- Opt-in user-labelled people matching, stored locally and deletable in one action. It is probabilistic matching, not verified facial identification.
- Indexing, thumbnails, embeddings, OCR text, face counts, labels, and query vectors are not sent to OpenAI. Sending one selected original photo to Realtime is a separate, clearly labelled action with its own confirmation. Details in [docs/LOCAL-PHOTO-SEARCH.md](docs/LOCAL-PHOTO-SEARCH.md).

**Works with no API key**
- A deterministic mock path demonstrates the local capture, reminder, search, and confirmation flow and sends nothing to OpenAI. It does not perform a GPT-5.6 review.

### What Lumi deliberately does not do currently

No Gmail or Calendar access. No continuous monitoring or recording. No autonomous desktop control. No whole-drive scanning. No message sending without confirmation. No cloud sync.

---

## Judge path at a glance

This live hero demo needs an `OPENAI_API_KEY`; the no-key path is an offline simulation and does not call GPT-5.6.

1. Prepare the fictional appointment, scam message, and screenshot described under [Synthetic demo data](docs/DEMO-CHECKLIST.md#synthetic-demo-data). Use only the safe temporary folder created for this test.
2. Start Lumi with `npm.cmd run dev`, open the floating companion, and wait for **Listening**.
3. Choose the capture control, select the Notepad window under **Application windows**, then choose the capture control again.
4. Check the local preview, then choose **Review this capture with GPT-5.6**. This second approval is the point at which the one retained capture may leave the device.
5. Verify the brief identifies the visible appointment date and time, preparation instruction, check-in step, and `example.com` link without inventing facts.
6. Choose **Open extracted link**, then **Cancel**. Verify Lumi reports that nothing was changed, opened, or sent.

The full repeatable path is in [docs/DEMO-CHECKLIST.md](docs/DEMO-CHECKLIST.md).

**Sample data:** the appointment, scam message, unique OCR marker, filenames, and optional person label in the checklist are synthetic. “Asha” is an example of a fictional label assigned by a tester; it is not a verified real identity. The mock path remains useful for offline confirmation testing, but its canned explanation is not a test of the appointment content.

---

## How GPT-5.6 is used

GPT-5.6 has two roles here: it designed and reviewed Lumi's architecture at build time (see [Building with Codex and GPT-5.6](#building-with-codex-and-gpt-56)), and it runs inside the shipped product. This section covers the runtime path.

GPT-5.6 is genuinely used at runtime — it is not a renamed Realtime model.

| Model | Exact role |
| --- | --- |
| `gpt-5.6-terra` | Default Responses API model for the separately confirmed screen brief and scam-warning assessment. `LUMI_REASONING_MODEL` can override it. |
| `gpt-realtime-2.1-mini` | Default live Realtime model for voice, text, and bounded tool proposals. `LIFELENS_REALTIME_MODEL` selects a supported alternative. |
| `gpt-4o-mini-transcribe` | Input transcription for the Realtime voice flow. |
| `clip-vit-base-patch32` | OpenAI's CLIP, run **locally** on ONNX Runtime for photo search. Never a network call. |

After you capture a chosen window, Lumi shows a second renderer confirmation. Only once you choose **Review this capture with GPT-5.6** does Electron main send that retained in-memory image to the Responses API.

The screen-brief request is deliberately constrained ([`src/main/services/screen-reasoning.ts`](src/main/services/screen-reasoning.ts)):

- `model: gpt-5.6-terra` by default, `reasoning.effort: low`, `store: false`
- Strict JSON Schema output (`lumi_screen_brief`, `strict: true`, `additionalProperties: false`)
- A developer instruction restricting the model to facts visible in the image, with no tool calls, no credential requests, no inferred private information, and no local file paths
- A 30-second timeout and a 900-token output ceiling
- A hashed `OpenAI-Safety-Identifier`

Main validates the closed schema — bounding every string and list length — before any of it reaches the UI. **GPT-5.6 has no desktop-control tools and cannot execute anything.** It returns text; the user decides what happens next.

The scam check uses the same explicit capture consent and default GPT-5.6 model, but a separate closed schema and validator in [`src/main/services/scam-check.ts`](src/main/services/scam-check.ts). Its four outcomes are risk levels, never guarantees. Local file search, CLIP photo search, OCR, face counting, people matching, reminder scheduling, and confirmation policy are not powered by GPT-5.6. Separately confirmed selected-photo analysis uses the Realtime model, not GPT-5.6.

The model and the strict structured-output call were verified against the live Responses API on 20 July 2026.

---

## Building with Codex and GPT-5.6

Lumi was created entirely inside the Build Week submission period. The repository's first commit is `044d701` on **17 July 2026**; the 35 commits currently in the repository end on 22 July in India (21 July Pacific), within the submission window. There is no pre-Build-Week codebase — `git log --reverse --date=iso-strict` is the timestamped evidence.

Codex's work is visible in the branch structure itself: `codex/lifelens-mvp` (merged as PR #1), `codex/lifelens-document-tools`, and `codex/lifelens-ui`.

### The workflow: developer-led planning, Codex-assisted delivery

GPT-5.6 was used twice over — once to help the developer explore and review Lumi’s design, and again inside the shipped product.

The developer used GPT-5.6 in ChatGPT to help pressure-test written specifications before implementation. Those specs are in the repository:

| Document | What it planned |
| --- | --- |
| [docs/plans/realtime-cost-reduction-phase-a.md](docs/plans/realtime-cost-reduction-phase-a.md) | Realtime cost reduction — scoped to Phase A, with Phase B explicitly fenced off as roadmap |
| [docs/LOCAL-PHOTO-SEARCH.md](docs/LOCAL-PHOTO-SEARCH.md) | The on-device CLIP stack: model pack, tokenizer, worker lifecycle, index format |
| [docs/UI-UX-POLISH.md](docs/UI-UX-POLISH.md) | Identity, window behaviour, file intake, panel layout, copy, accessibility |
| [docs/STATUS.md](docs/STATUS.md) | The per-slice delivery record, including where implementation departed from plan |

The loop ran like this:

1. **Plan with GPT-5.6.** The developer worked through the problem, set scope and safety constraints, and recorded a test plan.
2. **Inspect and implement with Codex.** Codex inspected the existing architecture, implemented against the brief, generated and ran tests, debugged failures, and reviewed permission- and privacy-sensitive flows.
3. **Review against the plan.** GPT-5.6 and Codex were used to check scope, missing requirements, and lifecycle gaps. [One such review](docs/reviews/realtime-cost-reduction-phase-a-review.md) flagged two medium-severity gaps that were then addressed.
4. **Record what actually happened.** STATUS.md captures verified command output and the decisions that departed from the plan, so the next planning session starts from reality.

Planning separately from implementation is what kept scope discipline enforceable. When a plan says *Phase B must not be implemented in this change*, the review has something concrete to check the diff against — and "no scope creep into the non-goal systems" becomes a verifiable claim rather than an intention.

### Where Codex accelerated the work

- **Scaffolding the vertical slice.** The Electron + React + TypeScript skeleton, the electron-vite configuration, and the first working capture → conversation loop came together in hours rather than days.
- **The security boundary.** Codex implemented the narrow typed `contextBridge` surface and the main-process IPC validators, then audited its own boundary for leaks — no generic IPC channel, no Node access in the renderer, no permanent key outside main.
- **The GPT-5.6 integration.** Building the Responses API call, the strict JSON Schema, the application-side validator that re-checks the closed schema, and the tests around all three.
- **The local vision stack.** The pinned-and-hashed model pack, the CLIP tokenizer reimplementation, the ONNX worker lifecycle, and the append-only index store — a large, fiddly subsystem with a lot of surface area for mistakes.
- **Testing and debugging.** Codex generated and ran unit, integration, security-boundary, and adversarial tests, then traced and corrected failures. Hostile person-label cases, for example, verify that labels are never interpolated as instructions.
- **Documentation and demo preparation.** Codex helped maintain environment guidance, third-party notices, release checks, the README, and the judge-facing walkthrough.

### Where I made the calls

Codex moved fast; the product decisions that shaped Lumi were mine.

I made the final architecture, scope, privacy, and interaction decisions and manually verified the important Windows user journeys. Codex output and model-generated tests were treated as reviewable engineering work, not as final authority.

- **The core trade-off.** The tempting build was an always-on assistant that watches the screen and acts on its own. I rejected it. One user-chosen window, captured once, reviewed only on explicit confirmation — that constraint defines the product, and I held it when it made features harder.
- **A second confirmation for GPT-5.6.** Capture and *send to a model* are different decisions and deserve different consent. Commit `a2d4adc` ("gate GPT-5.6 screen review explicitly") is where that became non-negotiable.
- **Text to the voice session, never the image.** The Realtime model receives the validated review, not the screenshot — so the strict schema stays the boundary that everything downstream depends on.
- **Vision stays on-device.** Photo search, OCR, and people search could have been cloud calls. Running CLIP locally cost real effort and is the reason Lumi can promise those images never leave the machine.
- **Scope discipline.** Gmail, Calendar, autonomous control, and cloud sync were all cut and stayed cut, so that what ships actually works.

### How the two fit together in the final product

GPT-5.6 appears at both ends of this project: it helped the developer reason about plans at build time, and it ships inside the product — reading one approved image and returning schema-bound text with no ability to act. Codex assisted with architecture inspection, implementation, tests, debugging, safety review, documentation, and demo preparation.

The division of labour held all week: GPT-5.6 helped explore and review, Codex helped build and verify, and every consequential product judgement stayed with the developer. The safety model is the clearest evidence it worked — a second confirmation before any screen capture reaches GPT-5.6, validated text rather than that screenshot to the voice session, and local-only indexing all survived from plan to shipped code.

The submission narrative is in [docs/BUILD-WEEK.md](docs/BUILD-WEEK.md).

---

## Architecture

| Layer | Responsibility |
| --- | --- |
| `src/renderer` | React UI, screen-source selection, visible confirmations, WebRTC lifecycle. Uses only `window.lifeLens`. |
| `src/preload` | The narrow, typed `contextBridge` API. Exposes no generic IPC and no Node APIs. |
| `src/main` | Screen capture, OpenAI requests, trusted pending-action execution, local storage, approved-folder controls, IPC validation. |
| `src/main/vision` | On-device CLIP inference in a separate utility process. Unreachable from the renderer or any model. |
| `src/shared` | Typed contracts and runtime payload validators shared across the boundary. |

**Stack:** Electron 38, React 19, TypeScript 5.9, electron-vite, Vitest, ONNX Runtime, Tesseract.js.

## Privacy and safety

**“Lumi’s default action is nothing.”** A suggestion is text until the user explicitly confirms an action.

| Boundary | What happens |
| --- | --- |
| Local/on-device | Approved-folder filename search, CLIP indexing and photo search, OCR, visible-face counting, probabilistic user-labelled people matching, local state, and reminder timers. Searches stay inside roots the user chose. |
| External reasoning | GPT-5.6 receives one retained screen capture only after the separate review or scam-check confirmation. Realtime receives voice/text and the validated text of a screen brief; a selected photo reaches Realtime only after its own confirmation. Optional Telegram sends go to Telegram after confirmation. |
| Confirmation required | GPT-5.6 screen review, scam capture/check, reminder creation, context saving, opening a file or URL, selected-photo analysis, Telegram sends, destructive index rebuilds, and people-data deletion. Main validates IPC payloads and revalidates trusted paths or identifiers before execution. |

Screen capture is always user initiated and never continuous. `OPENAI_API_KEY` stays in Electron main; the renderer receives only a short-lived Realtime credential and uses the typed `window.lifeLens` bridge. Retained screen captures are memory-bounded and not persisted by Lumi. Local people data can be deleted from Settings.

More detail: [docs/DECISIONS.md](docs/DECISIONS.md) and [docs/GOAL.md](docs/GOAL.md).

---

## Setup

**Requirements:** Windows 10 or 11 (x64), Git, npm, and Node.js **20.19+ or 22.12+** (the installed Vite/electron-vite toolchain requirement). A microphone is optional unless you want to test live voice. Node 24.16.0 and npm 11.13.0 were used for this final documentation audit.

```powershell
git clone https://github.com/satish9177/Lumi.git
Set-Location Lumi
npm.cmd ci
```

An OpenAI API key is required for live Realtime and GPT-5.6 review. Without one, Lumi runs its deterministic mock flow. For a one-shell judge run, set the key in PowerShell:

```powershell
$env:OPENAI_API_KEY = "your-key-for-this-shell-only"
npm.cmd run dev
```

During `npm.cmd run dev`, Electron main also loads ignored `.env` and `.env.local` files from the repository root. Inherited shell variables take precedence, and `.env.local` overrides values loaded from `.env`. Store secrets only in the process environment or those ignored local files — never in renderer code, `.env.example`, screenshots, logs, or commits.

| Variable | Required | Purpose |
| --- | --- | --- |
| `OPENAI_API_KEY` | Live features only | Mints the short-lived Realtime credential in main and authorizes confirmed GPT-5.6 requests. |
| `LUMI_REASONING_MODEL` | No | Defaults to `gpt-5.6-terra`. |
| `LUMI_REASONING_EFFORT` | No | Defaults to `low`; accepts `low`, `medium`, or `high`. |
| `LIFELENS_REALTIME_MODEL` | No | Defaults to `gpt-realtime-2.1-mini`. |
| `LIFELENS_REALTIME_REASONING` | No | Defaults to `low` for supported Realtime models. |
| `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` | Telegram only | Optional local Telegram client configuration. |

The canonical non-secret template is [.env.example](.env.example). Without `OPENAI_API_KEY`, Lumi uses its deterministic demo flow; it does not perform live screen reasoning, scam assessment, or Realtime inference.

### Development commands

```powershell
npm.cmd run typecheck
npm.cmd test
npm.cmd run build
npm.cmd run package
```

`npm.cmd run package` creates an unsigned Windows installer under `release/0.1.0/`. A trusted Authenticode signing step is required before distributing a production build — do not disable Windows security controls to run an unsigned package.

---

## For judges

Follow the [full judge checklist](docs/DEMO-CHECKLIST.md):

1. Start Lumi with an `OPENAI_API_KEY` for live provider-backed checks, or without one for the limited deterministic flow.
2. Create and approve only the safe temporary demo folder described there.
3. Capture the synthetic appointment, approve GPT-5.6 review separately, and check the actionable dates and preparation summary.
4. Create one reminder only after inspecting its confirmation; propose another and cancel it to verify that no second reminder is created.
5. Search the approved folder, open no result without confirmation, then optionally enable the local photo model and OCR to find the synthetic screenshot marker.
6. Capture the synthetic scam message, choose **Check this screen for scam warning signs**, then **Capture and check**. Treat the result as a risk assessment, not proof.

The live screen/scam path requires OpenAI API access. Realtime voice/text and natural-language OCR/people search requests also need the configured Realtime provider. Local photo/OCR/people models require one-time downloads and can take time to index; inference and matching then run locally.

## Known limitations

- The packaged Windows build is not yet code signed.
- Testing is Windows-focused; macOS and Linux are not supported submission platforms.
- A full live hero-scenario acceptance run still needs to be recorded five consecutive times.
- GPT-5.6 review is intentionally image-only and covers only a capture you explicitly approve; it does not read arbitrary local documents.
- Local model downloads and first indexing can take time. OCR is opt-in, and results can be incomplete while indexing is in progress.
- User-labelled people matching is probabilistic and may make mistakes; labels such as “Asha” are user-provided labels, not verified identities.
- Scam checks identify visible warning signs only. They cannot authenticate a sender or prove fraud or safety.
- A reminder proposal can be cancelled before creation, but this MVP does not provide deletion/cancellation for a reminder after it has been saved.
- External or state-changing proposals do nothing until confirmed; cancelling them leaves no partial action.
- The deterministic no-key path uses canned content to demonstrate local interaction and confirmation; it does not call GPT-5.6 or interpret the fictional appointment.

## Status

A public Build Week repository. Lumi is an MVP with a focused, working hero flow and documented release prerequisites. Current delivery notes are in [docs/STATUS.md](docs/STATUS.md).

## License

Lumi is released under the [MIT License](LICENSE).
Third-party model and dependency notices are in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
