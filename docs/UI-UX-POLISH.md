# Lumi UI/UX polish

A planned, not-yet-implemented pass over Lumi's identity, window behaviour,
file intake, panel layout, copy, and accessibility. Nothing in this document is
built. It exists so the work can be started safely once the local
semantic-photo-search work is complete and checkpointed.

## 1. Status and scope

**Status: implemented.** All four slices of the core MVP have landed. See
[docs/STATUS.md](STATUS.md) for the per-slice record, the verified command
numbers, and the decisions that departed from this document.

| Slice | Scope | State |
| --- | --- | --- |
| 1 | Branding, icon, draggable window, position persistence, display clamping, reset | Complete |
| 2 | Conversation-first panel, status selector, settings reorganisation | Complete |
| 3 | Secure single-file drag-and-drop, end to end through the existing confirmation flow | Complete |
| 4 | Copy consistency and accessibility | Complete |

Two findings corrected this document during implementation:

- **§6 assumed two profile directories.** Only `%APPDATA%\lifelens` exists, and
  `build.productName` never reached `app.getName()`. The rename was therefore a
  visible-branding change with no state migration. `appId`, `userData` and the
  internal identifiers deliberately still say `lifelens`.
- **§13's TTL was changed from idle to fixed.** `resolve` runs at proposal time
  as well as approval, so an idle timer would have let merely rendering a
  confirmation card extend the user's temporary grant.

Follow-up polish (§24) — tray, global shortcut, always-on-top toggle, toasts,
onboarding, edge snapping — remains deferred, as does everything under
"Deferred" in §24. A full manual Windows acceptance pass is still outstanding;
its checklist is at the end of docs/STATUS.md.

**Verified checkpoint (not a permanent number).** The last recorded verification
of the repository before this document was written:

| Command | Result |
| --- | --- |
| `npm.cmd test` | 483 passed, 2 skipped |
| `npm.cmd run typecheck` | passed |
| `npm.cmd run build` | passed |
| `npm.cmd run package` | passed, installer generated |

That is a point-in-time checkpoint of a tree that another session was actively
editing. It is **not** a target, a floor, or a permanent property of the
repository. Re-run all four commands and record the new numbers before and after
each phase below; do not copy these figures forward.

**In scope:** application identity and icon, window drag and position memory,
single-file drag-and-drop intake, the panel's internal layout, user-facing copy,
and accessibility.

**Out of scope:** the Realtime session lifecycle, the tool/confirmation security
model, the Telegram transport, and the local photo-search engine. This pass
restyles and re-arranges the surfaces those systems present; it changes none of
their behaviour.

## 2. Current UI assessment

The window is frameless, transparent, and always on top, toggling between an
88 × 88 orb and a 390 × 640 panel (`src/main/index.ts`). It is placed at the
bottom-right of the matching display's work area on every launch.

The panel is one scrolling column in `src/renderer/src/LifeLensApp.tsx`: question
form, capture picker, notices, capture card, explanation card, an approved-documents
workspace, an intelligent-photo-search section, a Telegram workspace, a
troubleshooting disclosure, confirmation cards, and — last, inside a collapsed
`<details>` capped at eight lines — the conversation transcript.

What is already right and must survive this pass:

- absolute paths stay in main; the renderer holds opaque identifiers only;
- `ToolConfirmationCard` is the single approval surface, and its content is
  authored in main from trusted state rather than supplied by the renderer;
- `PendingActionStore` applies a TTL and revalidates before execution;
- collapsing the panel mutes the microphone immediately and tears the session
  down on a grace timer;
- error text is bounded and honest — "That file changed since you reviewed it.
  Nothing was sent." is the house style, not an exception to it;
- `:focus-visible` outlines already exist on every interactive control.

The structural problems this pass addresses:

1. **Split identity.** The product is "Lumi" in conversational copy but
   "LifeLens" in the window title, header eyebrow, `productName`, `appId`,
   ARIA labels, CSS class prefixes, and the preload bridge name. `package.json`
   points `win.icon` at `build/icon.ico`, and that path does not exist — packaged
   builds therefore ship the default Electron icon.
2. **A dashboard, not an assistant.** Every capability is a permanently visible
   form. The conversation, which is the product, is the least prominent element
   on screen.
3. **No window memory.** Position is recomputed on every launch, so a user who
   moves Lumi loses that placement on restart.
4. **No file intake.** To act on a file the user is already looking at, they must
   approve its folder and search for it by name.
5. **Fragmented status.** The status row, the mode badge, per-button busy text,
   the Telegram state, and the photo-index state are five competing signals.
6. **Undiscoverable affordances.** The drag region, the fact that `×` collapses
   rather than quits, and demo mode are all unexplained.
7. **Small-type contrast.** 9–10 px secondary text at low contrast appears in
   badges, captions, and result metadata.
8. **Unconditional motion.** The orb pulses and controls lift on hover with no
   reduced-motion path.

## 3. Goals and non-goals

**Goals.** A conversation-first panel; a recognisable Lumi identity in the
installer, taskbar, and window; a window that can be moved deliberately and
remembers where it was put; a safe way to hand Lumi one local file; copy that is
short, calm, and honest; and keyboard, contrast, and reduced-motion support.

**Non-goals.** No new capability. No new network destination. No new file-access
path. No weakening of any confirmation. No redesign of local photo search — its
settings surface is adopted into the new structure as its owning session leaves
it. No renaming of the `window.lifeLens` bridge or the `lifelens-*` CSS prefixes
in this pass: that is churn across preload, tests, and another session's active
files for no user-visible benefit.

## 4. Visual direction and design tokens

Keep the existing dark navy surface with the cyan-to-violet accent — it is
distinctive and already encodes companion state — but formalise it into tokens
and change the composition from stacked forms to a conversation with inline
cards.

- **Mood.** Calm, luminous, glassy dark. One accent gradient
  (`#79efff` → `#a9abff`) reserved for primary actions and the orb. Every other
  surface is neutral navy.
- **Tokens.** `--surface-0/-1/-2`, `--stroke`, `--text-primary/-secondary/-muted`,
  `--accent`, `--accent-gradient`, `--state-listening/-thinking/-speaking/-success/-error`,
  `--radius-s/-m/-l` (8/12/16), `--space-1`…`--space-6` (4/8/12/16/20/24),
  `--font-size-xs`…`--font-size-l` (11/12/13/15), `--shadow-panel`.
- **Type.** No user-facing text below 11 px. Body 13 px / 1.45. One `h1`, in the
  header.
- **Density.** 12 px card padding, 8 px gaps, `--radius-m` corners — the current
  look with one rhythm instead of per-section values.
- **Motion.** 140–180 ms ease transitions. Every pulse, glow, and hover lift sits
  inside `@media (prefers-reduced-motion: no-preference)`.

**Where the tokens live.** They must **not** be appended to
`src/renderer/src/styles.css` while that file is being edited elsewhere (see
§19). Introduce them as a new `src/renderer/src/tokens.css` imported once from
`main.tsx`, then migrate existing rules onto the tokens during Phase 2. A new
file is safe to write at any time; editing `styles.css` is not.

## 5. Icon specification

**Concept — the Lumi orb.** A luminous sphere with a single off-centre spark: an
icon-ization of the in-app companion orb. No letterform. No face: the in-app eyes
do not survive 16 px and read as a toy in a taskbar.

- **Shape.** A circle at roughly 86% of the canvas on a fully transparent
  background. Radial gradient from cyan at the top-left through violet to magenta
  at the bottom-right, a soft white specular spark at about 30%/25%, and a 1 px
  darker rim (`#2a2f6e`, ~40% opacity) so the disc holds its edge against a light
  Windows taskbar and the installer's light chrome.
- **Detail ladder.** 256–1024 px: full gradient, inner glow, soft bloom on the
  spark. 48–64 px: no bloom, solid spark, slightly thicker rim. 16–32 px: flat
  two-stop gradient disc plus one white spark dot and nothing else.
- **Light and dark.** The mid-value gradient reads on both. The rim covers the
  light-taskbar case. Do not add a filled background plate.
- **Masters.** `icon-master.svg` as the source of truth, exported to sRGB
  transparent PNGs at 1024, 512, 256, 128, 64, 48, 32, 24, and 16 px.
- **Windows ICO** (`build/icon.ico`): layers at 256 (PNG-compressed), 128, 64,
  48, 32, 24, and 16 px. The 256 px layer is required — electron-builder rejects
  a Windows icon smaller than that, and Explorer's large view uses it.
- **Tray variant** (`build/tray/tray-16.png`, `tray-32.png`): the flat small
  variant with the rim raised to about 70% opacity for the dense tray context.
  The Windows tray is full-colour; no monochrome template is needed.
- **In-app.** A small inline SVG `BrandMark` component at 20 px in the header and
  48 px with glow in the empty state — SVG so it stays crisp at any display scale
  and can be themed, not an imported PNG.

No image asset is produced by this document. Assets are authored separately and
committed in the phase that consumes them.

## 6. Branding and migration decision

**Decision.** Renaming the application is treated as a single migration, not as a
configuration edit. The following change together, in one phase, with a
migration and a rollback path, or they do not change at all:

| Field | From | To |
| --- | --- | --- |
| `productName` | `LifeLens` | `Lumi` |
| `appId` | `com.lifelens.app` | `com.lumi.app` |
| `app.setAppUserModelId` | `com.lifelens.app` | `com.lumi.app` (must equal `appId`) |
| Window title / `<title>` | `LifeLens` | `Lumi` |
| NSIS shortcut and installer identity | `LifeLens` | `Lumi` |
| `userData` directory | `%APPDATA%\LifeLens` (packaged), `%APPDATA%\lifelens` (dev) | `%APPDATA%\Lumi` |

**Why this is a migration.** `app.getPath('userData')` is derived from
`productName`. Renaming it silently relocates every piece of local state the user
owns. Changing `appId` additionally makes NSIS treat this as a new application,
so a previously installed "LifeLens" entry remains in Add/Remove Programs until
it is uninstalled.

**State that must be preserved or migrated.** All of it lives under `userData`:

| State | Location | Consequence of losing it |
| --- | --- | --- |
| Reminders, approved folders, search results, saved contexts | `lifelens-state.json` | Reminders vanish; every folder must be re-approved |
| Telegram session | `telegram-session.bin` (`safeStorage`-encrypted) | User must re-scan a login QR code |
| Intelligent-photo-search preferences | `photo-search-preferences.json` | Feature reverts to off; consent is asked again |
| Downloaded CLIP model pack | `vision-models/clip-vit-base-patch32-q8/` | A 148 MB re-download |
| Local image index | `photo-index/` (`records.jsonl`, `vectors.bin`, `index-meta.json`) | A full re-index of the user's photos |
| Window position and always-on-top | `window-state.json` (introduced by §9) | Window returns to default placement |

**Required implementation.** Before `LocalStore` or any vision service is
constructed in `app.whenReady()`: if the new `userData` directory does not exist
and exactly one legacy directory (`LifeLens` or `lifelens`) does, move it with a
single `fs.rename`. A rename is atomic on the same volume and moves the 148 MB
pack and the index without copying. If the rename fails, do not fall back to a
partial copy — log a non-secret warning, continue with an empty profile, and
leave the legacy directory untouched so the user loses nothing permanently.
`safeStorage` on Windows is DPAPI-bound to the user account, not to a path, so
the Telegram session decrypts normally after the move; this must still be
verified manually.

**Rollback.** The rename is reversible while the legacy directory name is free.
Ship the migration behind a recorded pre-migration check: if the app finds both
directories, prefer the new one and leave the legacy one in place untouched. To
roll back a release, revert `productName`/`appId` and rename the directory back.
Do not delete the legacy directory in the migrating release — reclaim it, if
ever, in a later release once the migration is proven.

**Do not** change `productName` or `appId` in a phase that does not also contain
this migration, its tests, and a manual verification that reminders, the Telegram
session, approved folders, photo-search settings, the model pack, and the index
all survived.

## 7. Draggable-window behaviour

Dragging already works through `-webkit-app-region: drag` on the orb ring and the
panel header. The gaps are discoverability and a complete non-drag audit.

- **Drag surface.** The whole header bar, plus the orb's outer ring when
  collapsed.
- **Affordance.** A six-dot grip glyph at the header's leading edge in
  `--text-muted`, with a `Drag to move Lumi` tooltip. Note that CSS `cursor` is
  ignored inside a native drag region on Windows — the glyph is the affordance;
  do not try to change the cursor.
- **Non-draggable islands.** One rule rather than per-class annotations:
  `.drag-region :is(button, input, select, textarea, a, [role="button"]) { -webkit-app-region: no-drag; }`.
  Collapse, settings, and close stay clickable.
- **Window configuration.** No new `BrowserWindow` option is required;
  `frame: false` plus drag regions is already correct. `resizable` stays `false`
  for this pass.

## 8. Bottom-right expand/collapse anchoring

Today the panel keeps its **top-left** corner when it grows, so expanding an orb
that sits at the bottom-right pushes the panel toward the centre of the screen
after clamping.

Anchor the **bottom-right** corner instead:

```
newX = oldX + oldWidth  - newWidth
newY = oldY + oldHeight - newHeight
then clamp into the work area of the display that matches the current bounds
```

The orb appears to stay exactly where the user left it while the panel grows up
and to the left from it. This is a change to the geometry inside
`resizeWindowAtCurrentPosition` only. The `setPanelOpen` IPC channel, the
`expanded` effect, the collapse-time microphone mute, and the collapse-disconnect
timer are untouched.

## 9. Window-position persistence

Owned entirely by main. The renderer needs one new call, for reset.

- **New module `src/main/services/window-state.ts`**, persisting
  `{ version: 1, anchorX, anchorY, open, alwaysOnTop }` to `window-state.json` in
  `userData` — a separate file from `lifelens-state.json` so the two writers
  cannot race.
- **Store the bottom-right anchor**, not a rectangle. One stored point is then
  valid for both the orb and the panel size, and it matches the anchoring rule in
  §8.
- **Pure, exported, testable core:**
  `clampToDisplays(anchor, size, displays, fallback): Rectangle`. The stored
  position is honoured only if at least a 40 × 40 px region of the window's
  **header strip** (its top 40 px) intersects some display's work area.
  Otherwise the fallback is returned. That single predicate covers monitor
  removal, resolution change, and scale change — a window whose header is
  reachable can always be dragged back by hand.
- **Saving.** Subscribe to the window's `move` event, debounce 400 ms, write the
  anchor. Never write on every frame of a drag.
- **Startup.** Read state → derive bounds for the current size → clamp → apply.
  A missing or corrupt file falls back to the bottom-right of the **primary**
  display, which is today's behaviour.

## 10. Multi-monitor and DPI handling

- Electron reports `getBounds()` and `display.workArea` in the same DIP
  coordinate space, so no manual scale arithmetic is needed. Do not multiply by
  `scaleFactor`.
- Resolve the target display with `screen.getDisplayMatching(bounds)` — already
  the pattern in `positionWindow` — so a window on a secondary monitor clamps
  against that monitor's work area, not the primary's.
- Subscribe to `screen.on('display-removed')` and
  `screen.on('display-metrics-changed')` and re-run the clamp against the live
  window, so unplugging a monitor or changing scale mid-session can never strand
  Lumi off-screen.
- Verify at 100%, 150%, and 200% Windows scaling, and across a mixed-DPI pair of
  monitors, that the orb and panel keep a sane size and the header stays
  reachable.

## 11. Reset-position recovery

A user whose window is unreachable cannot use an in-window control, so recovery
must exist in more than one place.

- **New guarded IPC** `lumi:reset-window-position`, validated by the existing
  `requireMainWindow` check: clear the stored anchor, reposition to the default
  bottom-right of the primary display, show and focus the window.
- **Surfaces.** Settings → Appearance → "Reset window position", and the tray
  menu if the tray ships (§23). The tray entry is the one that works when the
  window itself cannot be reached.
- **Copy.** See `COPY.md` — the confirmation is "Lumi is back at the bottom-right
  of your main screen."
- Because the startup clamp in §9 already rejects an unreachable position, reset
  is a convenience for the live session, not the only safety net.

## 12. Secure drag-and-drop trust model

**Principle.** A drop is an explicit user gesture that creates one temporary
main-owned trusted item. It causes nothing else.

**Security invariants.** These are requirements on the implementation, not
aspirations:

1. A dropped file never causes an automatic action — no upload, no analysis, no
   Telegram send, no open.
2. Dropping a file does not approve its parent folder, and does not widen any
   search scope.
3. The dropped file's absolute path exists only in preload and main. Renderer
   application code never receives it.
4. The renderer receives an opaque identifier and safe metadata only: display
   name, type label, size, media kind, and optionally a locally rendered
   thumbnail.
5. Exactly one dropped file is retained, in memory, temporarily.
6. Every open, analyse, and send action revalidates the file immediately before
   it acts.
7. Existing confirmation behaviour is unchanged: the same
   `ToolConfirmationCard`, the same `PendingActionStore` approval, the same
   main-side native confirmation.
8. The existing Telegram and photo-analysis pipelines are reused as they stand.
9. No parallel or "unsafe" file-access pipeline is introduced anywhere.
10. This work must not weaken microphone hygiene on collapse.
11. This work must not weaken pending-action or attachment validation.

**Flow.**

1. **Renderer.** On `dragenter`/`dragover` carrying exactly one file, show the
   drop overlay. A multi-file drag shows the "one file at a time" message and
   does not accept. `document`-level `dragover` and `drop` handlers call
   `preventDefault()` unconditionally so a stray drop can never navigate the
   `webContents`.
2. **Preload.** A new bridge method resolves the dropped `File` to a path with
   `webUtils.getPathForFile(file)` and invokes the registration channel with it.
   The path is transient here and is never returned to the renderer. A drag whose
   item has no local path — a virtual file from Outlook or a browser — yields an
   empty string and is rejected with the copy in `COPY.md`.
3. **Main validation** (new `src/main/services/dropped-files.ts`):
   - `lstat` the path; reject symbolic links and Windows junctions, directories,
     and anything that is not a regular file;
   - reject `.lnk` and `.url` by extension **before** any sniffing, so a shortcut
     is never dereferenced;
   - `realpath` to canonicalise, then `lstat` the canonical path again to narrow
     the time-of-check/time-of-use window — the same pattern
     `attachment-validation.ts` already uses;
   - reuse the **existing** validators unchanged: `sniffAttachmentType` for the
     magic-byte check across JPEG, PNG, WebP, PDF, DOC, DOCX, and TXT;
     `MAX_ATTACHMENT_BYTES` (50 MB); `MAX_PHOTO_BYTES` (10 MB) and
     `isTelegramSafeDimensions` for images; `MAX_TEXT_BYTES` for text.
4. **Card.** The renderer shows the attachment card with Open, Ask Lumi, Analyse
   image (images only), Send via Telegram, and Remove. Nothing is pre-selected
   and nothing runs.
5. **Action.** Each action creates an ordinary `ToolProposal` and travels the
   existing confirmation path. Main revalidates at proposal time and again at
   approval time.

**Supported on first delivery:** JPEG, PNG, WebP, PDF, DOC, DOCX, TXT — exactly
the set `sniffAttachmentType` already recognises.

**"Ask Lumi" semantics.** An image routes to the existing `analyze_photo`
confirmation with the composer's text as the question. A document is answered
honestly: Lumi can open it or send it, and reading document contents is not
supported yet. No new content-extraction pipeline is introduced.

## 13. DroppedFileStore lifetime and revalidation

- **Capacity one.** A second drop replaces the first, and the card updates in
  place. There is no queue and no list.
- **In memory only.** Never written to disk, never added to
  `LocalStore.saveSearchResults`. Reusing the search-result store would let the
  next search silently evict the attachment and would wrongly imply that its
  folder had been approved.
- **Expiry.** A 30-minute idle TTL, refreshed whenever a confirmed action uses
  the entry. Cleared on Remove, on expiry, on `before-quit`, and whenever the
  entry fails revalidation.
- **Snapshot.** `{ droppedId, canonicalPath, fileName, sizeBytes, mtimeMs, sniffedType, mediaKind }`,
  frozen at registration — mirroring `TrustedAttachmentSnapshot`.
- **Revalidation.** Before every open, analyse, or send, re-`lstat` the canonical
  path and require the size, mtime, sniffed type, and media kind to match the
  snapshot exactly. Any mismatch aborts with the existing "changed since you
  reviewed it" outcome and clears the entry. This is deliberately the same rule
  `revalidateTrustedAttachment` applies to approved-folder results.

**The one seam this requires.** Today every trusted path resolves through
`resolveTrustedResultPath(store, resultId)`, which requires membership in an
approved root — which a dropped file intentionally does not have. Introduce a
main-only resolver:

```
resolveTrustedPath(id) :=
  droppedFiles.resolve(id)              // revalidates; not root-bound
  ?? resolveTrustedResultPath(store, id) // existing approved-root path, unchanged
```

Thread it through the four existing consumers — `createResultThumbnails`,
`validateTrustedAttachment`, `open_file` in `executeConfirmedTool`, and
`PendingActionStore.createTrustedPreview`. Because both identifier kinds are
UUIDs, no action contract changes shape: a dropped identifier travels as the
existing `resultId` / `fileResultId`. The preview builder labels the source
"Dropped file" instead of naming an approved folder, so the confirmation card
never implies folder trust that does not exist.

## 14. Main, preload, and renderer data boundaries

| Layer | May hold | Must never hold |
| --- | --- | --- |
| Main | Absolute canonical paths, snapshots, file bytes, the dropped-file store | — |
| Preload | A dropped path transiently, only to forward it to main | Any persistent path state; any path returned to the renderer |
| Renderer | Opaque IDs, display name, type label, size, media kind, locally rendered thumbnail data URLs | Absolute paths, directory names outside the safe relative path, file bytes |

The Realtime model sits outside all three: it receives no path, no thumbnail, and
no dropped-file metadata. It reaches a dropped file only through the same
explicit confirmation a user grants for an approved-folder result.

## 15. Conversation-first panel layout

Three fixed zones in the existing 390 × 640 panel, replacing the single scrolling
form column.

```
┌──────────────────────────────────────┐
│ ⠿ ◉ Lumi          ● Ready    ─ ⚙ ✕ │  Header, 44 px, drag region
├──────────────────────────────────────┤
│   [assistant message]                │
│   [inline card: photo results]       │  Conversation: fills, scrolls,
│                 [user message]       │  pinned to bottom, cards inline
│   [confirmation card]                │  in chronological order
├──────────────────────────────────────┤
│ [ dropped file chip              ✕ ] │  Attachment slot, when present
│ ┌──────────────────────────┐  🎙  ➤ │  Composer, 56 px
│ │ Ask Lumi…                │        │
└──────────────────────────────────────┘
```

**Header.** Grip glyph, `BrandMark`, the "Lumi" wordmark, a status pill, then
collapse-to-orb, settings, and close. The pill replaces the status row, the mode
badge, and the scattered busy text with one signal. Demo mode becomes a suffix in
the pill rather than a paragraph in the body. Status precedence, resolved by one
pure selector, highest first: **Needs attention → Offline → Sending → Searching →
Thinking → Listening → Speaking → Indexing photos → Ready.** Indexing is reported
only when Lumi is otherwise idle, so background work never masks a live state.

**Conversation.** The transcript is promoted from a collapsed disclosure to the
primary surface. User messages are right-aligned and accent-tinted; assistant
messages are left-aligned on a neutral surface; body text is 13 px / 1.45; paths
and URLs wrap with `overflow-wrap: anywhere`; file names and relative paths
render as inline code-styled chips. Scrolling stays pinned to the newest message
unless the user scrolls up, in which case a "Newest" affordance appears. A live
voice transcript renders as a dimmed in-progress message. Loading is a three-dot
pulse, or the static word "Thinking…" under reduced motion. Tool results become
compact system lines rather than full-width coloured notices. The empty state is
the brand mark, a one-line greeting, and three suggestion chips that prefill the
composer and never execute on their own.

**Result cards.** One shared card shell — surface, `--radius-m`, 12 px padding,
leading thumbnail or glyph, title, meta line, action row — specialised for files,
photos, Telegram recipients, reminders, confirmations, dropped files, and
photo-index progress. Every card keeps its opaque identifier and the existing
confirmation flow. `ToolConfirmationCard` is restyled onto the shell with its
logic frozen.

**Composer.** Text input with Enter to send and Shift+Enter for a newline; a
microphone button with idle, listening, and connecting states where a click while
listening cancels listening through the existing listening/collapse hygiene; a
gradient send button that explains why it is disabled; and the attachment chip
row above it when a dropped file is present. Capture and folder approval move out
of the top level into a composer menu and Settings respectively.

## 16. Proposed component hierarchy

```
src/renderer/src/
  App.tsx                     rename of LifeLensApp.tsx; orchestration only
  status.ts                   new  pure deriveStatus(inputs) -> StatusDescriptor
  copy.ts                     new  every user-facing string
  tokens.css                  new  design tokens (see §4)
  components/
    BrandMark.tsx             new  orb SVG, 16-48 px
    PanelHeader.tsx           new  drag region, status pill, window controls
    StatusPill.tsx            new  pure descriptor -> pill
    ConversationView.tsx      new  message list, empty state, scroll pinning
    MessageBubble.tsx         new
    ResultCard.tsx            new  shared card shell
    FileResultCard.tsx        new
    PhotoResultGrid.tsx       existing; restyle only, after its owner lands
    RecipientPicker.tsx       new  extracted from the Telegram workspace
    ToolConfirmationCard.tsx  existing; restyle only, logic frozen
    ExplanationCard.tsx       existing; restyle only
    DroppedFileCard.tsx       new
    IndexProgressCard.tsx     new  consumes the photo-search status
    Composer.tsx              new
    DropOverlay.tsx           new
    SettingsView.tsx          new  plus one component per group
    ToastHost.tsx             new  transient messages, aria-live
```

State stays where it is — App-level hooks and refs. This pass extracts
presentation. `file-search-controller.ts`, `pending-action-coordinator.ts`, and
`realtime.ts` are not restructured.

## 17. Settings structure

A slide-over panel over the conversation, opened from the header gear, grouped by
capability rather than by subsystem:

| Group | Contains |
| --- | --- |
| Voice | Voice on/off, what demo mode means |
| Telegram | Connect, login, two-step password, logout — the whole current workspace moves here |
| Files and approved folders | The approved-root list, add, revoke |
| Intelligent photo search | The existing photo-search card, adopted as its owning session leaves it |
| Privacy | A plain summary of what stays on this device |
| Appearance | Reset window position, always-on-top, a note on reduced motion |
| About | Version, licences, the Telegram non-affiliation disclosure |

Model overrides, environment variables, and other technical knobs stay out of the
UI entirely.

## 18. Accessibility requirements

- **Keyboard.** A complete tab order from header to conversation to composer.
  Escape closes layers in order: drop overlay, settings, capture picker, then
  collapse. Enter and Space activate a card's primary action. The photo grid uses
  a roving tab index.
- **Focus.** The existing `:focus-visible` treatment extends to every new
  component through a token; no component may remove an outline without
  replacing it.
- **Screen reader.** One polite live region for status and one assertive region
  for errors, replacing today's scattered notices. `role="log"` on the
  conversation. An explicit label on every icon button — "Collapse to orb",
  "Settings", "Hide Lumi". The dropped-file card announces the file and, plainly,
  that no action has been taken.
- **Reduced motion.** Orb pulse, glow, hover lift, and typing dots are all gated
  behind `prefers-reduced-motion: no-preference`, with static equivalents
  otherwise.
- **Contrast.** No user-facing text below 11 px. Muted text reaches at least
  4.5:1 against its surface. State is never conveyed by colour alone — the status
  dot always carries a text label, which is already true and must stay true.
- **High contrast.** Under `forced-colors: active`, every control must remain
  visible and bounded.

## 19. Coordination snapshot

> **Time-sensitive. Re-check before any implementation.** This section records
> what another session's working tree contained at the moment this document was
> written. It is an uncommitted, in-flight observation — not architecture, not a
> permanent property of the repository, and very likely already out of date.
> Re-run `git status --short` and `git diff --stat` and rebuild this picture
> before starting any phase.

Observed at authoring time, on top of commit `f4f2e5d feat: add local photo
search foundation`, with the local semantic-photo-search work uncommitted:

| File | Nature of the in-flight change |
| --- | --- |
| `src/shared/contracts.ts` | Photo-search IPC channels, `PhotoSearchStatus`, `concepts`, result `reason` |
| `src/shared/search-query.ts` | Concept normalisation and limits |
| `src/preload/index.ts` | Photo-search bridge methods, `removeDocumentRoot`, `setRealtimeActive` |
| `src/main/index.ts` | Handler wiring for the above |
| `src/main/services/document-search.ts` | Ranking fusion and reason labels |
| `src/main/services/store.ts` | Root removal support |
| `src/main/vision/**` | New `coordinator`, `scanner`, `semantic-search` modules |
| `src/renderer/src/LifeLensApp.tsx` | Photo-search settings section, root revoke, concept extraction |
| `src/renderer/src/components/PhotoResultGrid.tsx` | Reason presentation |
| `src/renderer/src/realtime.ts` | Updated capability instructions |
| `src/renderer/src/styles.css` | Photo-search settings, progress, root-list, and danger-button styles |
| `docs/LOCAL-PHOTO-SEARCH.md` | Status rewritten from "not yet built" to a delivered Phase 1 path |

Two facts in this snapshot corrected earlier assumptions and are worth carrying
forward as lessons rather than as data:

- `src/renderer/src/styles.css` **is** in the conflict set. Design tokens must
  therefore go in a new file (§4), not appended to it.
- A settings surface, approved-root revocation, and result reason labels already
  exist. The polish pass **adopts** them into the new structure; it does not
  design them from scratch.

## 20. Conflict analysis with the semantic-photo-search work

**Hard conflicts — do not touch until that work is complete and checkpointed:**

- `src/shared/contracts.ts`
- `src/preload/index.ts`
- `src/main/index.ts`
- photo result components (`PhotoResultGrid.tsx`)
- the settings UI
- `LifeLensApp.tsx` / the `App.tsx` restructuring
- `src/renderer/src/realtime.ts`
- the thumbnail and path-resolution seams (`thumbnails.ts`, `document-search.ts`)
- pending actions (`pending-actions.ts`)
- attachment validation (`attachment-validation.ts`)
- `src/renderer/src/styles.css`

**Coordinated — small, mechanical, apply between that session's commits:**
`package.json` (the build block and the migration in §6) and the window-state
wiring in `src/main/index.ts`, which is roughly ten lines but lands in a hard-conflict
file and so must be sequenced, not parallelised.

**Safe now — new files and documents that cannot collide:** icon assets and
`build/`, this document, `docs/COPY.md`, `src/renderer/src/tokens.css`,
`src/main/services/window-state.ts` and its tests, and unwired renderer
components (`BrandMark`, `StatusPill`, `DropOverlay`, `DroppedFileCard`).

`AGENTS.md` assigns `src/shared`, `src/main`, `src/preload`, root configuration,
and integration to the primary agent. Phases 2 through 5 below must therefore run
as the primary agent after integration, never concurrently with another session
in the same files.

## 21. Safe implementation phases

**Phase 0 — safe now, no conflict.** Author icon assets outside the repository;
land this document and `docs/COPY.md`; create `tokens.css`; build
`window-state.ts` with its tests; create `BrandMark`, `StatusPill`,
`DropOverlay`, and `DroppedFileCard` as unwired components.

**Phase 1 — branding migration, coordinated window.** The `package.json` build
block, `setAppUserModelId`, window title and `<title>`, the icon files, and the
`userData` migration with its rollback path and tests — all in one phase, per §6.

**Phase 2 — window behaviour, coordinated window.** Wire `window-state.ts` into
main, add the bottom-right anchoring from §8, the display-change subscriptions
from §10, and the reset IPC and its Settings entry from §11.

**Phase 3 — after the photo-search work lands.** The panel restructure: header,
conversation, composer, and the settings shell that adopts the existing
photo-search card. Then the status selector, the copy sweep, and the
accessibility sweep.

**Phase 4 — after Phase 3.** Drag-and-drop end to end: preload bridge, contracts,
`DroppedFileStore`, the `resolveTrustedPath` seam, and card wiring.

**Phase 5 — follow-up polish, only if time remains.** Tray icon and menu, global
shortcut, always-on-top toggle, toasts, onboarding conversation, edge snapping.

Run `npm.cmd run typecheck`, `npm.cmd test`, `npm.cmd run build`, and
`npm.cmd run package` at the end of every phase, and record the actual numbers in
`docs/STATUS.md` rather than reusing the checkpoint in §1.

## 22. Automated test requirements

**`window-state.test.ts`** — the clamp honours a position whose header strip
remains reachable; falls back when the stored display is gone; falls back on a
missing or corrupt file; bottom-right anchor arithmetic is correct for both the
orb and the panel size; the debounced save round-trips.

**`dropped-files.test.ts`** — accepts each of the seven supported types from
magic-byte fixtures; rejects a directory, a symbolic link, a `.lnk`, a file whose
content contradicts its extension, a file over 50 MB, an image over 10 MB, an
image with unsafe dimensions, and an empty path from a virtual file; the TTL
expires; a second drop replaces the first; revalidation fails after an mtime
change.

**Seam tests** — `createResultThumbnails`, `validateTrustedAttachment`,
`open_file`, and the `analyze_photo` preview each succeed for a dropped
identifier and still fail for an unknown one; a dropped file never enters
`LocalStore.saveSearchResults`; a dropped file never causes a parent folder to
appear in the approved-root list.

**`status.test.ts`** — the full precedence table from §15.

**Renderer tests** — the composer's disabled-state reasons; the drop overlay
appears for a single-file drag and not for a multi-file drag; Escape closes
layers in order; removing the dropped card clears main-side state.

**Copy lint** — a test asserting that no string in `copy.ts` contains a banned
term from `docs/COPY.md`.

**Regression floor** — the existing suite must stay green at every phase. It is
the evidence that confirmation behaviour, attachment validation, and microphone
hygiene survived a purely visual pass.

## 23. Manual Windows test plan

1. **Branding and migration.** `npm.cmd run package`; confirm the icon in the
   installer, the executable, the taskbar, Alt-Tab, the Start menu, and
   Add/Remove Programs, at 100%, 150%, and 200% scaling. Install over an existing
   LifeLens profile and verify that reminders, approved folders, the Telegram
   session, photo-search settings, the CLIP pack, and the photo index all
   survived. Then verify the rollback path.
2. **Window.** Drag by the header and by the orb ring; confirm buttons still
   click inside the drag region. Restart and confirm the position is restored.
   Move to a secondary monitor, unplug it, relaunch, and confirm Lumi returns to
   the primary display. Change resolution and scale while running. Hand-edit
   `window-state.json` to an off-screen anchor and confirm both the startup clamp
   and the reset action recover it.
3. **Drag and drop.** Each supported type from Explorer; a folder; a `.lnk`; a
   symbolic link created with `mklink`; a 60 MB file; two files at once; an
   Outlook attachment dragged directly. After every drop, confirm that nothing
   happened. Then confirm Open, Analyse, and Send each raise their existing
   confirmation. Modify the file between the card and the confirmation and
   confirm the "changed" outcome. Confirm Remove and quit both clear it.
4. **Panel.** Collapse and expand and confirm the orb does not move and the
   microphone still mutes. Run a voice turn. Use the empty-state chips. Confirm
   scroll pinning during a long answer. Reach every settings group, and complete
   the Telegram login from its new location.
5. **Accessibility.** A complete keyboard-only pass. NVDA announces status
   changes, errors, and drop results. Windows "Show animations" off stops the
   pulse. High Contrast leaves every control visible.

## 24. MVP versus deferred features

**Core MVP.**

1. Lumi icon and visible branding, with the §6 migration.
2. A discoverable draggable window.
3. Window-position persistence and display clamping.
4. A reset-window-position action.
5. Secure one-file drag-and-drop.
6. The header / conversation / composer layout.
7. The settings reorganisation.
8. Better copy.
9. Keyboard navigation, contrast, and reduced motion.

**Follow-up polish — only if implementation time remains.** Tray icon and tray
menu; a global keyboard shortcut; an always-on-top toggle; toasts; the onboarding
conversation; edge snapping.

**Deferred — explicitly not in this pass.** Multiple-file drop; dropped-document
content question-answering; a resizable window; a light theme; auto-update UI;
renaming the preload bridge and CSS identifiers; drag-out support; folder
dropping.

## 25. Implementation order

1. Phase 0: icon assets, this document, `COPY.md`, `tokens.css`,
   `window-state.ts` and tests, unwired components. **Safe today.**
2. Phase 1: branding migration — configuration, icons, `userData` move,
   rollback, verification.
3. Phase 2: window-state wiring, bottom-right anchoring, display-change handling,
   reset action.
4. **Wait** for the semantic-photo-search work to be complete and checkpointed.
5. Phase 3: panel restructure and status selector.
6. Phase 3: copy sweep and accessibility sweep over the new shell.
7. Phase 4: drag-and-drop end to end.
8. Phase 5: follow-up polish, in the §24 order, only if time remains.
9. A full manual Windows pass, and the four verification commands.

The rule behind the ordering: anything touching `src/shared`, `src/preload`,
`src/main/index.ts`, `styles.css`, or the renderer files listed in §20 waits for
step 4. Everything before it is new-file or asset work that cannot collide.

## 26. Completion checklist

- [ ] Coordination snapshot (§19) re-checked against a live `git status`.
- [ ] Icon renders correctly at 16, 32, 48, 128, and 256 px on light and dark
      taskbars.
- [ ] `productName`, `appId`, and `setAppUserModelId` agree.
- [ ] Migration verified: reminders, approved folders, Telegram session,
      photo-search settings, CLIP pack, and photo index all survive an upgrade.
- [ ] Rollback path exercised at least once.
- [ ] Drag affordance visible; every control inside the drag region still
      clickable.
- [ ] Expanding and collapsing leaves the orb visually in place.
- [ ] Position survives restart; an off-screen anchor is recovered on startup.
- [ ] Monitor removal and scale change while running leave the header reachable.
- [ ] Reset-position works, including from outside the window.
- [ ] A dropped file causes no action until confirmed.
- [ ] Dropping does not approve the parent folder.
- [ ] No absolute path reaches renderer application code.
- [ ] Every open, analyse, and send revalidates and fails closed on a change.
- [ ] One dropped file retained, in memory, with a working TTL.
- [ ] Microphone still mutes on collapse.
- [ ] Existing confirmation and attachment validation behaviour unchanged.
- [ ] No banned term from `docs/COPY.md` appears in user-facing text.
- [ ] Keyboard-only pass complete; NVDA announces status and errors.
- [ ] Reduced motion honoured; no text below 11 px; muted text at 4.5:1.
- [ ] `typecheck`, `test`, `build`, and `package` all pass, with the **new**
      numbers recorded in `docs/STATUS.md`.
