# Lumi MVP goal

Lumi is a small Windows desktop companion that turns one user-chosen screen capture into a concise explanation and proposes only bounded follow-up actions that the user can inspect and confirm.

## Hero scenario

The committed fictional hospital appointment is visible in a browser. The user opens Lumi, chooses that window, captures it once, separately approves a GPT-5.6 review, and receives a structured brief of the visible appointment time, preparation, link, risks, and next actions. The user can then inspect a reminder or link proposal and either confirm it or cancel with no partial action.

## Current MVP boundary

Included: a draggable transparent always-on-top companion; compact interaction panel; live Realtime voice and text; on-demand display/window capture; separately approved GPT-5.6 screen review; structured dates, links, risks, and next actions; confirmation-gated reminders, approved-folder search, file and URL opening, local context saving, selected-photo analysis, and optional Telegram attachment sending; on-device semantic photo search, OCR, visible-face counting, and user-labelled people matching; small local stores; Windows x64 packaging.

Excluded: continuous monitoring, arbitrary or autonomous computer control, Gmail or Calendar access, payments, credential or OTP handling, mobile clients, cloud sync, whole-drive scanning, and unconfirmed external actions.

## Original vertical-slice gate

The first delivery gate was:

`launch -> click companion -> connect -> capture one screen -> explanation -> confirmed mock reminder`

That gate is complete. Mock mode remains available when `OPENAI_API_KEY` is absent so local capture and confirmation paths can be tested deterministically, but it does not call GPT-5.6 or interpret the fictional appointment.
