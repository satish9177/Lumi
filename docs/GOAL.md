# LifeLens MVP goal

LifeLens is a small Windows desktop companion that can take a user-requested screen capture, discuss it in a natural voice conversation, identify useful follow-up information, and request safe, confirmed actions.

## Hero scenario

An interview email is visible. The user opens LifeLens, asks what the email is about, receives a concise spoken and visible explanation of the interview date and preparation, accepts a reminder proposal, searches a previously approved folder for a resume, opens the selected resume, and later receives a reminder whose source explains why it exists.

## MVP boundary

Included: a draggable transparent always-on-top companion; compact interaction panel; WebRTC Realtime voice session; on-demand display/window capture; image explanation; dates/links/next-action extraction; five bounded local tools; explicit confirmation; a small local data store; Windows packaging.

Excluded: continuous monitoring, arbitrary computer control, message sending, payments, credentials, OTPs, mobile clients, cloud sync, and whole-drive scanning.

## First vertical-slice gate

Before any parallel work, the primary agent must make this path work in the app and pass typecheck and build:

`launch -> click companion -> connect voice -> capture one screen -> explanation -> confirmed mock reminder`.

Mock mode is allowed for deterministic local smoke testing when `OPENAI_API_KEY` is absent. It must exercise the same UI, IPC confirmation, capture, and reminder-storage paths as live mode.
