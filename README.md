# Lumi

**A private-by-default desktop companion that turns one screen you choose into a safe, actionable brief.**

Built for **OpenAI Build Week 2026** with Codex and GPT-5.6.
Category: *Apps for Your Life*

- **Demo video:** _add the public YouTube URL before submitting_
- **Codex `/feedback` session ID:** _add the primary session ID before submitting_
- **Repository:** https://github.com/satish9177/Lumi

---

## The problem

The thing you need to act on is usually already on your screen — an interview email with a date, a portal with a deadline, a form with a link you have to open before Friday. The details are visible but scattered, and the next step is buried in the middle of a paragraph.

The obvious fix is an assistant that watches your desktop and clicks things for you. That trade is bad: continuous screen recording and autonomous computer control are a lot of risk for a small convenience.

Lumi takes the narrow version of that bet. You choose one window. You capture it once. You explicitly approve a review. You get a structured brief, and every follow-up action asks first.

## What Lumi does

**One approved screen, reviewed by GPT-5.6**
- A floating Windows companion that stays out of the way.
- You pick a single screen or application window and capture it once — Lumi never watches continuously.
- A separate, visible confirmation (**Review this capture with GPT-5.6**) is required before the image leaves your machine.
- GPT-5.6 returns a validated brief: a summary, visible dates, safe `http`/`https` links, risks, and suggested next actions.

**Live conversation**
- Voice and text through the OpenAI Realtime API over WebRTC.
- The Realtime session receives the *validated text* of a screen review, never the screenshot itself.

**Actions that always ask first**
- Reminders, approved-folder document search, opening a returned file, opening an extracted link, and optional Telegram attachment sending.
- Every one is confirmation-gated in the UI and revalidated in the main process before it runs. Rejecting does nothing, silently and completely.

**On-device vision — nothing leaves the machine**
- Semantic photo search powered by CLIP ViT-B/32 running locally on ONNX Runtime (CPU).
- Local OCR (Tesseract) and visible-face counting.
- User-labelled people enrolment and search, stored locally and deletable in one action.
- No image, thumbnail, embedding, or query vector from this subsystem is ever sent to OpenAI or anywhere else. Details in [docs/LOCAL-PHOTO-SEARCH.md](docs/LOCAL-PHOTO-SEARCH.md).

**Works with no API key**
- A deterministic mock path demonstrates the full interaction and confirmation flow and sends nothing to OpenAI.

### What Lumi deliberately does not do

No Gmail or Calendar access. No continuous monitoring or recording. No autonomous desktop control. No whole-drive scanning. No message sending without confirmation. No cloud sync.

---

## Try it in two minutes

1. Start Lumi and open the floating companion.
2. Keep an interview email or event page visible — something with a date, a preparation request, and a link.
3. Select that window, then capture it once.
4. Choose **Review this capture with GPT-5.6** in the confirmation card.
5. Read the structured brief: summary, dates, links, risks, next actions.
6. Confirm a reminder or a link if it's useful. **Reject one first** to see that rejection performs no action.

The full repeatable path is in [docs/DEMO-CHECKLIST.md](docs/DEMO-CHECKLIST.md).

**Sample data:** none required. Any email, calendar invite, or event page on screen exercises the hero flow. Without an API key, the deterministic mock path runs the same interaction end to end.

---

## How GPT-5.6 is used

GPT-5.6 has two roles here: it designed and reviewed Lumi's architecture at build time (see [Building with Codex and GPT-5.6](#building-with-codex-and-gpt-56)), and it runs inside the shipped product. This section covers the runtime path.

GPT-5.6 is genuinely used at runtime — it is not a renamed Realtime model.

| Model | Exact role |
| --- | --- |
| `gpt-5.6-terra` | The confirmed screen review, through the Responses API. |
| `gpt-realtime-2.1-mini` | Default live Realtime model for voice, text, and bounded tool proposals. `LIFELENS_REALTIME_MODEL` selects a supported alternative. |
| `gpt-4o-mini-transcribe` | Input transcription for the Realtime voice flow. |
| `clip-vit-base-patch32` | OpenAI's CLIP, run **locally** on ONNX Runtime for photo search. Never a network call. |

After you capture a chosen window, Lumi shows a second renderer confirmation. Only once you choose **Review this capture with GPT-5.6** does Electron main send that retained in-memory image to the Responses API.

The request is deliberately constrained ([`src/main/services/screen-reasoning.ts`](src/main/services/screen-reasoning.ts)):

- `model: gpt-5.6-terra`, `reasoning.effort: low`, `store: false`
- Strict JSON Schema output (`lumi_screen_brief`, `strict: true`, `additionalProperties: false`)
- A developer instruction restricting the model to facts visible in the image, with no tool calls, no credential requests, no inferred private information, and no local file paths
- A 30-second timeout and a 900-token output ceiling
- A hashed `OpenAI-Safety-Identifier`

Main validates the closed schema — bounding every string and list length — before any of it reaches the UI. **GPT-5.6 has no desktop-control tools and cannot execute anything.** It returns text; the user decides what happens next.

The model and the strict structured-output call were verified against the live Responses API on 20 July 2026.

---

## Building with Codex and GPT-5.6

**Codex `/feedback` session ID:** _add the primary session ID before submitting_

Lumi was created entirely inside the Build Week submission period. The repository's first commit is `044d701` on **17 July 2026**; all 31 commits fall between 17 and 21 July 2026. There is no pre-Build-Week codebase — `git log --reverse` is the timestamped evidence.

Codex's work is visible in the branch structure itself: `codex/lifelens-mvp` (merged as PR #1), `codex/lifelens-document-tools`, and `codex/lifelens-ui`.

### The workflow: GPT-5.6 plans, Codex builds

GPT-5.6 was used twice over — once to design Lumi, and again inside the shipped product. The build-time half is the reason the code came together as fast as it did.

Every substantial subsystem started as a written specification worked out with GPT-5.6 in ChatGPT before a line of it was implemented. Those specs are in the repository:

| Document | What it planned |
| --- | --- |
| [docs/plans/realtime-cost-reduction-phase-a.md](docs/plans/realtime-cost-reduction-phase-a.md) | Realtime cost reduction — scoped to Phase A, with Phase B explicitly fenced off as roadmap |
| [docs/LOCAL-PHOTO-SEARCH.md](docs/LOCAL-PHOTO-SEARCH.md) | The on-device CLIP stack: model pack, tokenizer, worker lifecycle, index format |
| [docs/UI-UX-POLISH.md](docs/UI-UX-POLISH.md) | Identity, window behaviour, file intake, panel layout, copy, accessibility |
| [docs/STATUS.md](docs/STATUS.md) | The per-slice delivery record, including where implementation departed from plan |

The loop ran like this:

1. **Plan with GPT-5.6.** Work the problem until the design is settled and written down — current verified behaviour separated from proposed changes, scope boundaries drawn, test plan and budgets specified up front.
2. **Hand the spec to Codex.** The Phase A plan names its own audience: *"A Codex implementation session. This document is intended to be sufficient without repeating the architectural investigation."* Codex implemented against a brief, not a vague prompt.
3. **Review with GPT-5.6.** The implementation went back for review against the plan — checking for scope creep, unimplemented requirements, and lifecycle gaps. [One such review](docs/reviews/realtime-cost-reduction-phase-a-review.md) approved the diff while flagging two medium-severity lifecycle gaps for Codex to address.
4. **Record what actually happened.** STATUS.md captures verified command output and the decisions that departed from the plan, so the next planning session starts from reality.

Planning separately from implementation is what kept scope discipline enforceable. When a plan says *Phase B must not be implemented in this change*, the review has something concrete to check the diff against — and "no scope creep into the non-goal systems" becomes a verifiable claim rather than an intention.

### Where Codex accelerated the work

- **Scaffolding the vertical slice.** The Electron + React + TypeScript skeleton, the electron-vite configuration, and the first working capture → conversation loop came together in hours rather than days.
- **The security boundary.** Codex implemented the narrow typed `contextBridge` surface and the main-process IPC validators, then audited its own boundary for leaks — no generic IPC channel, no Node access in the renderer, no permanent key outside main.
- **The GPT-5.6 integration.** Building the Responses API call, the strict JSON Schema, the application-side validator that re-checks the closed schema, and the tests around all three.
- **The local vision stack.** The pinned-and-hashed model pack, the CLIP tokenizer reimplementation, the ONNX worker lifecycle, and the append-only index store — a large, fiddly subsystem with a lot of surface area for mistakes.
- **Test coverage as a habit.** The suite grew alongside the features to **76 files and 1,422 passing tests**, including adversarial cases such as hostile person-labels that must never be interpolated as instructions.
- **Documentation and release hygiene.** Environment defaults, third-party notices, `.gitignore` coverage for keys and databases, and the judge-facing docs.

### Where I made the calls

Codex moved fast; the product decisions that shaped Lumi were mine.

- **The core trade-off.** The tempting build was an always-on assistant that watches the screen and acts on its own. I rejected it. One user-chosen window, captured once, reviewed only on explicit confirmation — that constraint defines the product, and I held it when it made features harder.
- **A second confirmation for GPT-5.6.** Capture and *send to a model* are different decisions and deserve different consent. Commit `a2d4adc` ("gate GPT-5.6 screen review explicitly") is where that became non-negotiable.
- **Text to the voice session, never the image.** The Realtime model receives the validated review, not the screenshot — so the strict schema stays the boundary that everything downstream depends on.
- **Vision stays on-device.** Photo search, OCR, and people search could have been cloud calls. Running CLIP locally cost real effort and is the reason Lumi can promise those images never leave the machine.
- **Scope discipline.** Gmail, Calendar, autonomous control, and cloud sync were all cut and stayed cut, so that what ships actually works.

### How the two fit together in the final product

GPT-5.6 appears at both ends of this project. It designed and reviewed the architecture at build time, and it ships inside the product — reading one approved image and returning a schema-bound brief with no ability to act on it. Codex turned those specifications into a working Electron application and the 1,422 tests that hold its boundaries in place.

The division of labour held all week: GPT-5.6 decided *what should exist and why*, Codex made it real, and every consequential product judgement stayed mine. The safety model is the clearest evidence it worked — a second confirmation before any image reaches a model, validated text rather than screenshots to the voice session, and vision that never leaves the device are all constraints that survived from plan to shipped code, because there was a written plan for the review to check the diff against.

The submission narrative is in [docs/BUILD-WEEK.md](docs/BUILD-WEEK.md).

---

## Architecture

| Layer | Responsibility |
| --- | --- |
| `src/renderer` | React UI, screen-source selection, visible confirmations, WebRTC lifecycle. Uses only `window.lifeLens`. |
| `src/preload` | The narrow, typed `contextBridge` API. Exposes no generic IPC and no Node APIs. |
| `src/main` | Screen capture, OpenAI requests, native confirmations, local storage, approved-folder controls, IPC validation. |
| `src/main/vision` | On-device CLIP inference in a separate utility process. Unreachable from the renderer or any model. |
| `src/shared` | Typed contracts and runtime payload validators shared across the boundary. |

**Stack:** Electron 38, React 19, TypeScript 5.9, electron-vite, Vitest, ONNX Runtime, Tesseract.js.

## Privacy and safety

- Screen capture is always user initiated. Lumi never continuously watches the desktop.
- `OPENAI_API_KEY` stays in Electron main. The renderer receives only a short-lived Realtime credential and cannot reach Node or Electron APIs.
- GPT-5.6 receives an image only after a separate, visible confirmation. The retained capture is memory-bounded and never persisted.
- Every external or state-changing action is confirmation-gated in the renderer and revalidated in main before execution.
- Document search stays inside folders you explicitly approve; file opening is limited to results from those searches.
- The local vision subsystem sends nothing anywhere, and all people data can be deleted in one action.

More detail: [docs/DECISIONS.md](docs/DECISIONS.md) and [docs/GOAL.md](docs/GOAL.md).

---

## Setup

**Requirements:** Windows 10 or 11 (x64), a current Node.js LTS release, and npm.

```powershell
git clone https://github.com/satish9177/Lumi.git
Set-Location Lumi
npm.cmd install
```

An OpenAI API key is required for live Realtime and GPT-5.6 review. Without one, Lumi runs its deterministic mock flow. Set secrets only in your local process environment or another ignored local file — `.env.example` documents every supported variable and contains no credentials.

```powershell
$env:OPENAI_API_KEY = "your-key-for-this-shell-only"
npm.cmd run dev
```

`LUMI_REASONING_MODEL` defaults to `gpt-5.6-terra` and `LUMI_REASONING_EFFORT` to `low`; both can be overridden. Optional Telegram variables are documented in [.env.example](.env.example). Never commit `.env`, session files, logs, or local databases.

### Development commands

```powershell
npm.cmd run typecheck
npm.cmd test        # 76 files, 1,422 tests
npm.cmd run build
npm.cmd run package
```

`npm.cmd run package` creates an unsigned Windows installer under `release/0.1.0/`. A trusted Authenticode signing step is required before distributing a production build — do not disable Windows security controls to run an unsigned package.

---

## For judges

Use the [demo checklist](docs/DEMO-CHECKLIST.md), and confirm these boundaries while testing:

- **No key:** the mock interaction stays usable and sends nothing to OpenAI.
- **Live key:** a user-selected capture requires the GPT-5.6 review confirmation before any Responses API call.
- **Rejection:** a rejected reminder, file open, URL open, or Telegram send performs no action at all.
- **Scope:** folder search returns only results beneath a user-approved folder.
- **Local vision:** photo and people search complete with no network activity.

## Known limitations

- The packaged Windows build is not yet code signed.
- A full live hero-scenario acceptance run still needs to be recorded five consecutive times.
- GPT-5.6 review is intentionally image-only and covers only a capture you explicitly approve; it does not read arbitrary local documents.
- The deterministic no-key path demonstrates the interaction and confirmation flow but does not call GPT-5.6.

## Status

A public Build Week repository. Lumi is an MVP with a focused, working hero flow and documented release prerequisites. Current delivery notes are in [docs/STATUS.md](docs/STATUS.md).

## License

Lumi is released under the [MIT License](LICENSE).
Third-party model and dependency notices are in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
