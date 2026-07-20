# Lumi copy guide

How Lumi talks. This is the reference for every string a user can read: labels,
status, notices, errors, confirmations, and empty states.

Companion document: [docs/UI-UX-POLISH.md](UI-UX-POLISH.md). The copy sweep is
part of that pass and is not yet implemented; the intended home for these strings
is a single `src/renderer/src/copy.ts` module so they can be reviewed and linted
in one place.

## Voice and tone

**Short.** Two sentences at most. If a third sentence is needed, the interface is
explaining something the design should have made obvious.

*Clarified during the Slice 4 implementation.* Several recommended strings below
run to three sentences, so the rule as written and the rule as exemplified
disagreed. The resolution: a third sentence is permitted **only** when it carries
a distinct recovery step, or the reassurance that nothing happened — never to add
detail. Four is never permitted. `src/renderer/src/copy.ts` is linted against
this rule by `copy.test.ts`, which names each permitted exception and why.

**Calm.** Nothing is urgent unless the user's data is at stake. No exclamation
marks, no "Oops", no "Uh oh", no apologising twice.

**Plain.** Everyday words. If a sentence contains a word the user would not say
out loud to a friend, replace it.

**Honest about uncertainty.** Say what Lumi knows, and say what it does not.
"Lumi couldn't confirm this arrived" is better than silence and far better than a
checkmark. Never round an unknown up to a success.

**One clear next action.** End with the single thing the user can do. If there is
nothing to do, say what is true and stop — do not invent a step.

**No blame.** The user did not do anything wrong. Lumi is the actor in every
sentence about failure: "Lumi can't reach Telegram", never "You didn't connect
Telegram".

**Never claim an action succeeded until it is confirmed.** "Sent" appears only
after the send is acknowledged. Until then it is "Sending…". If acknowledgement
never arrives, it is "couldn't confirm" — not "sent", and not "failed".

### Structure

State what happened, then what is true now, then the one next action — in that
order. Put the reassurance where it matters: when something did not happen,
say so explicitly, because "nothing was sent" is information the user actively
wants.

### Grammar and person

Lumi is "Lumi", not "I" and not "we". The user is "you". Use sentence case for
everything except proper nouns. Do not capitalise feature names that are not
proper nouns — "intelligent photo search", not "Intelligent Photo Search", except
as a settings-group heading.

## Banned user-facing terminology

These describe Lumi's internals. They must never appear in text a user can read.
They remain correct in code, comments, and documentation.

| Banned | Say instead |
| --- | --- |
| IPC | (nothing — describe the outcome) |
| renderer, main process | Lumi |
| token | "code" for a login code; "usage" or "quota" for billing. Genuine billing and API-usage text may say "tokens" |
| orchestrator, controller, coordinator | (nothing — describe the outcome) |
| trusted result | "the file", "this photo" |
| fail-closed, fails closed | "Lumi stopped and did nothing" |
| credential, ephemeral credential | "connection", or omit |
| mock mode, mock voice | demo mode |
| LifeLens | Lumi |
| pending action, approval ID | "this action", "what you confirmed" |
| vector, embedding, index row | "what Lumi has learned about your photos" |
| generation, session generation | (nothing — describe the outcome) |
| root, approved root | approved folder |
| sniff, magic bytes | "check the file" |
| ONNX, CLIP, model pack | "the local photo model", or a plain size in MB |

Also banned: raw exception text, stack frames, error codes, HTTP status numbers,
file system error strings, and absolute paths. A message that reaches a user must
have been written by a person. `messageFrom` already strips stack frames — that
is a safety net, not a licence to pass an exception through.

## Recommended copy

Each entry is the full user-facing string. Adapt the numbers, never the shape.

### Microphone permission failure

> Lumi can't use your microphone. Allow it in Windows Settings → Privacy →
> Microphone, then tap the mic again. You can keep typing meanwhile.

### OpenAI key unavailable

> Lumi's voice and answers need an OpenAI key, and there isn't one set up.
> Everything local still works.

### Demo mode

> Demo mode — voice and answers are simulated. Everything else works normally.

Where a longer explanation fits, such as Settings → Voice:

> Demo mode — voice and answers are simulated, so Lumi can be tried without an
> OpenAI key. Capture, search, confirmation, and Telegram all behave exactly as
> they will live.

### Telegram disconnected

> Telegram isn't connected. Connect it in Settings → Telegram to send messages or
> files.

### File missing

> Lumi can't find that file anymore. It may have been moved or renamed — search
> again to refresh the list.

### Folder not approved

> That file is outside the folders you've shared with Lumi. Approve its folder
> once, and Lumi can search it from then on.

### File changed before confirmation

> This file changed after you reviewed it, so nothing was sent. Check it and
> confirm again.

For a non-send action, keep the shape and change the verb:

> This file changed after you reviewed it, so Lumi stopped. Check it and confirm
> again.

### Uncertain Telegram delivery

> Lumi couldn't confirm this message arrived. Check the chat in Telegram before
> sending it again.

This is the most important string in this document. It must never be softened
into a success or hardened into a failure.

### Photo indexing incomplete

> Lumi is still learning your photos (1,240 of 3,000). Results may be incomplete
> for now — searching by name and date still works fully.

### Semantic photo search unavailable

> Photo search by content isn't available right now. Lumi can still find photos
> by name, folder, and date.

### Unsupported dropped file

> Lumi can't take this file type yet. It works with JPEG, PNG, WebP, PDF, Word,
> and text files.

### File too large

> This file is 62 MB — Lumi handles files up to 50 MB, and photos up to 10 MB.
> Nothing was added.

### No reliable search result

> Nothing matched that closely. These are the most recent near-matches — or try
> different words.

When there is nothing at all to show:

> Nothing matched that. Try different words, or approve another folder in
> Settings → Files.

### Network unavailable

> Lumi is offline. Local file search still works — voice and Telegram will
> reconnect when the network is back.

### Confirmation required

On the confirmation card:

> Lumi only acts when you confirm. Nothing happens if you cancel.

For an action that leaves the device, name the destination plainly:

> This one photo will be sent to OpenAI so Lumi can answer. No other photo leaves
> your computer.

### Virtual file drop with no usable local path

> This file isn't saved on your computer yet. Save it somewhere first, then drop
> it on Lumi.

### Multiple files dropped when only one is supported

> One file at a time, please. Drop a single file and Lumi will pick it up.

### Window position reset

> Lumi is back at the bottom-right of your main screen.

### People (labelled-person matching)

Privacy notice, shown before and after enabling:

> Lumi can match faces you label. Face matching stays on this device and may
> make mistakes.

Match reasons are app-authored from a tier and the user's own label, never a
raw score:

> Likely match for Father

> Possible match for Father

> Likely matches for Mother and Father

There is no phrase asserting identity. Never "This is Father", never "Father
confirmed", never "Definitely Father", never "Certain match" — the underlying
measurement cannot support that claim, and stating it would be a lie the user
might act on.

Coverage that is incomplete says so rather than reading as absence:

> Some photos haven't been checked for Father yet.

An unrecognised name states the fact and stops — it never offers to create the
profile, because enrolment is a flow the user starts, not one a search talks
them into:

> You haven't created a profile called Father yet.

### Model download or verification failure

Download interrupted:

> The download stopped before it finished. Nothing was installed — try again when
> you're ready.

Verification failed:

> The download didn't arrive intact, so Lumi discarded it. Nothing was installed.
> Try again.

Verification failure is never retried automatically and never resumed from what
arrived. The copy says "discarded" because that is exactly what happens.

## Strings to retire

These exist today and should be replaced during the copy sweep.

| Today | Replace with |
| --- | --- |
| "Demo mode is active because no API key is configured. It exercises the same capture and confirmation path." | The demo-mode entry above |
| "Voice paused to save cost — ask a question to reconnect." | "Voice is paused to save battery and quota — ask anything to reconnect." |
| "…then scan the current login QR token." | "…then scan this code. It refreshes automatically." |
| "See it. Understand it." (header tagline) | Removed; the status pill occupies that space |
| Every remaining "LifeLens" in user-facing text | "Lumi" |
| Duplicated "Ask Lumi" label and button | One label |

## Checklist for new copy

- [ ] Two sentences or fewer.
- [ ] No banned term, no raw exception, no path, no error code.
- [ ] If something did not happen, it says so.
- [ ] Success is claimed only when confirmed.
- [ ] Exactly one next action, or none.
- [ ] Lumi is the actor in any sentence about failure.
- [ ] Reads calmly out loud.
