# Local intelligent photo search

Photos are embedded and searched **on this device**. No image, thumbnail,
embedding, or query vector is sent to OpenAI or anywhere else.

Status: Phase 1 semantic search, Phase 2 local OCR and visible-face counting,
and Phase 3 user-labelled people matching are implemented. Video, RAW/HEIC,
and cloud-folder analysis remain deliberately out of scope. People matching is
labelled as uncertain, may make mistakes, and still requires a manual Windows
acceptance pass with non-personal test images before release claims go beyond
the automated checks described below.

## Shape of the system

```
main process
├── vision/manifest.ts      the one allowlisted model pack (pinned, hashed)
├── vision/model-pack.ts    consent, download, verify, install, clear
├── vision/tokenizer.ts     CLIP byte-level BPE, reimplemented locally
├── vision/engine.ts        worker lifetime, queueing, idle release, restarts
├── vision/index-store.ts   journal + flat vector file
└── vision-worker.ts        utilityProcess: ONNX Runtime, CPU only
```

Nothing above is reachable from the renderer or from the Realtime model. The
renderer never receives a path, a pixel buffer, a vector, or a vector row.

## The model pack

One pack, `clip-vit-base-patch32-q8`, pinned to a specific Hugging Face commit
(not a branch, so the bytes cannot change underneath us):

| file | bytes | role |
| --- | --- | --- |
| `vision_model_quantized.onnx` | 89,117,001 | image tower |
| `text_model_quantized.onnx` | 64,504,507 | text tower |
| `vocab.json` | 862,328 | tokenizer vocabulary |
| `merges.txt` | 524,619 | tokenizer merge ranks |

**148 MB total**, disclosed before the user consents. CLIP ViT-B/32 by OpenAI,
ONNX export by Xenova, MIT licensed.

Installed at `%APPDATA%\lifelens\vision-models\clip-vit-base-patch32-q8\`.

Every URL, filename, size, and SHA-256 is a compile-time constant in
`manifest.ts`. There is no parameter anywhere for a caller-supplied URL, hash,
filename, or path — not from the renderer, IPC, Realtime, model output, or
conversation. `MODEL_ASSETS` is frozen, and the allowlist is re-checked
immediately before each request.

Download behaviour: staged to a temporary directory, resumed with `Range` where
the server permits, SHA-256 verified, then atomically renamed into place. A
digest mismatch is discarded and **never** retried, so a resume can never build
on a corrupt prefix. Transport faults retry three times with backoff.

## The index

`%APPDATA%\lifelens\photo-index\`

- `records.jsonl` — append-only; a later line supersedes an earlier one
- `vectors.bin` — fixed 512-float rows, row *N* at `N * 2048`
- `index-meta.json` — format and model versions, row count

Phase 1 records hold only local metadata: image id, root id, root-relative path, name,
mtime, size, dimensions, vector row, model version, status, a bounded failure
code, attempts, updated time. **No absolute paths, image bytes, thumbnails, OCR,
Phase 3 face data, or OpenAI data.** Absolute paths stay main-owned and are rebuilt from
the live approved-root store on demand.

Crash safety comes from ordering, not transactions: the vector is written and
flushed first, then a journal line claims that row. A crash between the two
leaves an orphan row (reclaimed by compaction); a crash mid-line leaves a torn
line (dropped on load). Neither can produce a record pointing at bytes that were
never written. A journal referencing a row beyond the vector file, or a model
version mismatch, discards the index and rebuilds — a stale vector is worse than
no vector, because it produces confidently wrong matches.

Compaction rewrites both files once ≥30% of rows are dead (minimum 64 rows).

## Runtime behaviour

Ordinary startup loads **nothing** — no ONNX Runtime, no model. The worker
starts on the first embedding request. The image tower is released after 90 s
idle; the text tower stays resident so a spoken query does not pay a cold load.
Exactly one inference runs at a time. A crashed worker is restarted at most 3
times in 5 minutes, after which the feature degrades and filename search
continues.

## Running the tests

```
npm.cmd test
```

`real-inference.test.ts` runs actual ONNX inference and is **skipped** unless the
pack is installed. It checks the property that shape assertions cannot: that the
locally reimplemented tokenizer and both towers land in the *same* CLIP space,
by asserting relative similarity orderings rather than absolute values.

## User-facing Phase 1 path

The renderer exposes narrow app-authored actions to enable the feature, consent
to and control the frozen model download, pause/resume indexing, clear/rebuild,
disable, and choose the plugged-in-only preference. It never supplies a URL,
hash, filename, destination, model path, photo path, or vector.

The scanner accepts JPEG, PNG, and WebP only. It rejects links/reparse paths,
oversized files, malformed headers, and images above 50 megapixels before
decode. Electron creates a bounded aspect-preserving thumbnail; main performs a
center crop and sends only the exact 224 × 224 BGRA bitmap to the worker.

The coordinator reconciles `(root id, relative path, mtime, size)` snapshots
with the journal, rechecks authorization and metadata at every dequeue, and
checks again after inference before committing the row. Revoking a root cancels
and discards active work for that generation, removes its queued entries, and
purges its records. Transient locked/inference failures retry three times;
permanent failures retry only after the file changes.

Visual queries use the two app-authored prompts `a photo of {concept}` and
`{concept}`. Their local text vectors are averaged and normalized in memory.
Ranking uses semantic 0.80, filename/folder 0.15, and recency 0.05, with named
cosine honesty gates at 0.27 (strong) and 0.20 (possible). Results below the
possible gate are never claimed to depict the concept.

The index only nominates results. Main reconstructs each path from the live root
store and revalidates containment, regular-file status, mtime, size, and link
status before registering a new opaque trusted result. Existing thumbnail,
open, selected-photo analysis, and Telegram confirmation flows remain the only
ways to act on those identifiers.

---

# Phase 2 — text and visible-face counting

Both run **on this device**. No image, page of text, face box, or count is sent
to OpenAI or anywhere else.

## The extras pack

A second pack, `photo-search-extras`, kept deliberately separate from the CLIP
pack so that installing it cannot change `MODEL_PACK_ID` and invalidate a single
stored vector.

| file | bytes | licence | role |
| --- | --- | --- | --- |
| `eng.traineddata` | 4,113,088 | Apache-2.0 | Tesseract English |
| `face_detection_yunet_2023mar.onnx` | 232,589 | MIT (Shiqi Yu) | YuNet detection |

**4.3 MB total.** Both pinned to immutable commits and SHA-256 verified before
install, by the same audited downloader the CLIP pack uses — `model-pack.ts` was
generalized over a pack descriptor rather than copied, so there is one place
where a digest is checked, not two.

YuNet finds *where* faces are so Lumi can count them. It cannot recognise who
anyone is, and this phase builds nothing that could.

## Index schema

Phase-2 fields are additive and independently versioned:

```
ocrStatus  ocrText  ocrTokens  ocrVersion  ocrFailureCode  ocrAttempts
faceStatus  visibleFaceCount  uncertainFaceCount  faceVersion  faceFailureCode
```

`INDEX_FORMAT_VERSION` stays at 2, so a Phase-1 journal loads unchanged with
every vector intact. `OCR_MODEL_VERSION` and `FACE_MODEL_VERSION` are compared
*per record* and are deliberately absent from `index-meta.json`: a new text
model drops stale text and keeps the vector, rather than re-embedding the
library to gain a better reader.

`undefined` is a real fourth state — "never checked". It is never reported as
zero faces or as no text.

Still not stored: image bytes, face crops, face embeddings, landmarks, identity,
absolute paths, or anything from OpenAI.

## OCR

Tesseract.js in its own worker, terminated when idle so its WASM heap never sits
resident beside the CLIP image tower. Images are decoded to a bounded greyscale
PNG (longest edge 1600, never upscaled). One job at a time, 25 s ceiling,
cancellable, and it fails closed if the verified training data is missing rather
than fetching one.

**Extracted text is untrusted.** It is matched against and nothing else: never
logged, never placed in an error, never sent to Realtime, never treated as an
instruction. Only app-authored reasons such as "Contains the text you searched
for" ever leave the device.

Match ladder, strongest first: exact phrase, all tokens, bounded fuzzy, none.
Digits must match exactly — returning someone else's reference number as though
it were the one asked for is not a tolerable failure. A partial match is not a
match.

## Visible-face counting

YuNet through the existing ONNX worker as a third model kind, letterboxed to
640x640 (padded, never cropped — a crop cuts people out of the edges of a group
photo). Decode and NMS happen worker-side and the geometry is discarded there:
**only a bounded list of confidence scores crosses the process boundary.** The
`kps_*` landmark tensors are never read.

Thresholds: >= 0.90 confident, >= 0.60 uncertain, below that rejected. The two
counts are kept separate so an unsure detection can never be presented as a
person.

Query semantics: `eq n`, `gte n` (group), `none`. "None" requires both counts to
be zero. An unscanned image is never treated as zero and is reported as coverage.

Wording stays literally true — "2 visible faces detected", never "2 people".
Someone turned away or behind another guest is present but undetected.

## Ranking

Weights when every signal exists: semantic 0.55, OCR 0.30, filename 0.10,
recency 0.05, renormalized over whatever is actually present. People count is a
hard filter, not a weight: a photo of one person is not a slightly worse answer
to "two people", it is the wrong answer.

Tie-breaking is unchanged: fused score, then modified time, then normalized path.

## Scheduler

Embeddings first (Phase-1 search must stay usable while Phase 2 catches up),
then face scanning, then OCR at the lowest duty cycle. One expensive image task
at a time. Pause, revocation, disable, and shutdown abort in-flight Phase-2 work
immediately.

# Phase 3 — user-labelled people

Matching faces against people the **user has explicitly labelled**. This is not
identity discovery: nothing is named automatically, no profile is created
without an explicit confirmation, and there is no lookup of any kind against
anything outside this device.

## The people pack

A third pack, independent of the CLIP pack and the Phase-2 extras pack, so
installing it cannot invalidate a stored CLIP vector, an OCR result, or a
visible-face count.

| | |
| --- | --- |
| Pack id | `photo-search-people` |
| Model | SFace (MobileFaceNet trained with the SFace loss) |
| Source | `opencv/opencv_zoo`, `models/face_recognition_sface` |
| Pinned revision | `f12e12798e8314f7c074a6656816c048dcc95b7a` |
| File | `face_recognition_sface_2021dec.onnx` |
| Size | 38,696,353 bytes |
| SHA-256 | `0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79` |
| Licence (code **and** weights) | Apache 2.0 |
| Redistribution | Permitted; Lumi downloads rather than redistributes |
| Input | `data`, `[1, 3, 112, 112]`, planar BGR, 0-255, no mean subtraction |
| Output | `fc1`, `[1, 128]`, **not** L2-normalized (measured norm ≈ 4.83) |
| Measured CPU latency | 31 ms median (min 20, max 38), Windows x64 |
| Model load | 961 ms |

The revision is the same commit already pinned for YuNet, so the detector
producing the landmarks and the recognizer consuming them come from one
immutable snapshot.

### Why this model

OpenCV Zoo licenses **per model** — its root README says "Please refer to
licenses of different models" — and the SFace directory carries its own Apache
2.0 `LICENSE` plus the statement "All files in this directory are licensed under
Apache 2.0 License". That sentence is what grants the weights, not merely the
surrounding Python.

InsightFace ArcFace was rejected on its own words: its model zoo states "ALL
models are available for non-commercial research purposes only" despite
MIT-licensed code. FaceNet (davidsandberg) publishes no weight licence at all.

### The provenance caveat

The SFace weights descend from models trained on CASIA-WebFace, VGGFace2 and
MS-Celeb-1M; the model card does not say which produced this export.
MS-Celeb-1M was later withdrawn by Microsoft over the provenance of its images.
Apache 2.0 grants use and redistribution of the artifact and says nothing about
how those training faces were collected. This was raised explicitly and accepted
as a recorded decision. See THIRD_PARTY_NOTICES.md.

Two smaller residuals: the upstream `zhongyy/SFace` repository carries no
licence file, so the grant rests on OpenCV's redistribution; and the ONNX
conversion was performed by an OpenCV maintainer from that source.

## Independent versioning

`FACE_EMBED_MODEL_VERSION` and `PEOPLE_INDEX_VERSION` are separate numbers, and
separate again from the CLIP, OCR and face-count versions. Bumping the embedding
model marks profiles as needing re-enrolment without destroying them; bumping
the index version invalidates stored match outcomes but not the enrolment.

## What is stored, and what is not

Stored, in `%APPDATA%/lifelens/people/profiles.json`, whole-file encrypted
through Electron `safeStorage` (DPAPI on Windows):

- an opaque profile id, the user's chosen label, and its case-folded form
- 3-8 L2-normalized 128-float reference embeddings per profile
- reference quality metadata, timestamps, model and index versions

Never stored: reference image bytes, reference image paths, face crops, face
landmarks, raw library-face embeddings, similarity scores, absolute paths.

If `safeStorage` reports encryption unavailable, the store **refuses to write**
rather than falling back to plaintext.

`people/` is its own directory outside `photo-index/`, so "delete all people
data" is a single recursive removal that provably cannot take a CLIP vector or
an OCR result with it.

## Alignment

Five YuNet landmarks are fitted to the standard ArcFace 112x112 reference
template with a least-squares **similarity** transform — rotation, uniform scale
and translation only. A similarity cannot shear or stretch, so a bad landmark
cannot distort a crop into resembling someone else. Sampling is bilinear with
edge clamping, because a black wedge across a cheek is an artefact the model
would otherwise encode as a feature of that person.

Landmark decoding lives in `face-landmarks.ts`, deliberately separate from
Phase 2's `face-detect.ts`. The counting path's guarantee that it never reads
the `kps_*` tensors is enforced by a test, and Phase 3 keeps that true rather
than widening a module whose narrowness was the point.

## Matching tiers

Thresholds are anchored on OpenCV's published same-identity cosine threshold for
these exact weights, **0.363**, rather than on a number chosen by feel.

| Tier | Condition | Copy |
| --- | --- | --- |
| `likely` | score ≥ 0.45 and no caution applies | "Likely match for Father" |
| `possible` | score ≥ 0.363, or ≥ 0.45 with a caution | "Possible match for Father" |
| `none` | score < 0.363 | "No reliable match found" |
| unscanned | not yet processed | "Not checked for Father yet" |

There is deliberately no tier meaning "definitely them". Cautions — low
resolution, uncertain detection, a profile at the bare minimum reference count,
or another enrolled person scoring within 0.05 — only ever **demote**. No
combination can promote a weak score.

A profile's score against a face is the **maximum** over its references, not the
mean: references are varied by design, so a true match agreeing strongly with
one and weakly with the rest is the expected shape.

## Worker protocol

Two Phase-3 commands, both closed and both validated on each side.

`detect_faces_detailed` takes the same bounded 640×640 letterboxed bitmap as
Phase 2's `detect_faces` and answers with boxes and landmarks. It exists as a
*separate* command precisely so that `detect_faces` can keep answering with
scores alone: visible-face counting must stay unable to locate a face inside a
photograph, and it stays that way because its response type has nowhere to put a
box. Geometry reaches main only — never the index, never the renderer as
coordinates, never Realtime.

`embed_faces` takes already-aligned 112×112 tensors and nothing else. There is
no field for a path, a model identifier, a profile id or a label, so no caller —
and nothing a caller was handed by the renderer — can steer which model runs or
which file is read. Values outside 0-255, non-finite values, partial tensors and
counts beyond the ceiling are all refused rather than clamped.

Every embedding is L2-normalized inside the worker, and the receiving parser
re-checks the norm before accepting the event. SFace does not return a unit
vector, and an un-normalized vector would make every cosine comparison quietly
wrong, so the check exists on both sides.

## Enrolment

A profile exists only because someone typed a name, chose specific photos,
pointed at a specific face in each, and confirmed. No other path creates one —
not dropping a file, not analysing an image, not a Telegram contact, not a
search for a name that does not exist yet.

The largest face is never assumed to be the intended one. When a reference holds
more than one usable face, enrolment stops and asks; a parent standing behind a
child is the ordinary case, and guessing enrols the wrong person under a name
the user will then trust.

Source files are revalidated **twice** — when a reference is added and again at
final confirmation — because the bytes the user reviewed are the only bytes they
consented to. A file that changed, disappeared, or whose folder was revoked in
between is refused.

Candidate previews and candidate ids are memory-only, bounded, and expire with
the draft. Once the profile is created the draft is discarded, which is what
makes "no reference paths are stored" true rather than aspirational.

Quality gates: minimum face size, minimum detector confidence, successful
alignment, and a cross-reference consistency check that refuses an enrolment
whose references look like different people.

## Query contract

`people_labels`, at most three names, each bounded to 40 characters.

Labels are text the user said — nothing more. The contract rejects profile ids
(including UUID-shaped strings), long hex identifiers, paths, vectors,
structured data and control characters, because each is something a caller might
offer in place of a name, and a sanitized attack is still an attack that got
partway.

Resolution to a profile id happens **in main only**, exactly and
case-insensitively. There is no fuzzy matching: "Mum" and "Mum's sister" are
different people, and a near miss that silently returned the wrong one is the
worst failure this feature has. An unrecognised label is reported as missing.

Filename matches and biometric matches stay separate signals. A photo named
`father-birthday.jpg` does not become a face match.

## Per-photo match records

Additive fields on the same `PhotoIndexRecord`, independently versioned from
every Phase-1 and Phase-2 field: `peopleStatus`, `peopleModelVersion`,
`peopleIndexVersion`, `peopleFailureCode`, `peopleAttempts`, and
`peopleMatches`. A match entry is exactly
`{ profileId, status, matchingFaces, profileRevision }` — bounded, closed on
parse, and structurally unable to carry a similarity value, a face box, a
landmark, a reference path, or a label. Every one of those is app metadata the
scan pipeline never has in scope by the time it writes a record.

Status is one of `not_checked`, `checking`, `likely`, `possible`,
`checked_no_reliable_match`, `failed_retryable`, `failed_permanent`, or
`profile_unavailable`. Only the terminal six are ever written; `not_checked`
and `checking` are computed by `people-records.ts:resolveMatch` from *absence*
or from the coordinator's live in-flight set, never persisted — a persisted
"checking" would survive a crash and become a permanent lie.

A completed scan writes an explicit outcome for every profile it considered,
including a negative one, which is what makes absence of a row mean
"not checked" rather than being ambiguous with "checked and it wasn't them."
A profile created after a photo was already scanned is, correctly, absent from
that photo's `peopleMatches` — and `resolveMatch` reads that as `not_checked`,
not as a negative.

Each profile carries a `revision`, bumped when its references change (not on
rename). A stored match's `profileRevision` is compared against the live
profile's; a mismatch resolves to `not_checked`. This is the entire mechanism
behind "adding a reference invalidates only that profile's records" and
"renaming does not require a rescan" — both are consequences of one comparison,
not separate code paths that could drift apart.

## Scan pipeline and embedding lifetime

For every eligible indexed photo, once matchable profiles exist: decode
(bounded, revalidated), detect with `detect_faces_detailed`, gate each face on
size and detector confidence, align with the same similarity transform
enrolment uses, batch-embed with `embed_faces`, score against every matchable
profile's references, and write bounded outcomes.

`people-scan.ts:scanPhotoForPeople` is the one place a face belonging to
someone **not** enrolled becomes a vector at all. The tensor, the batch, and
every embedding it produces are function-local; nothing above this function's
own stack frame can hold a reference to one. What survives the call is an
array of `{ profileId, status, matchingFaces, profileRevision }` rows — a type
that has no field to carry an embedding even if a future caller wanted one to.
This is a structural claim, not a policy one, and it is under test:
`people-scan.test.ts` serializes the full return value and asserts it never
contains an embedding, a landmark, or a score.

## Coordinator scheduling

Labelled-person matching sits at priority 4 in the existing single-worker
scheduler — after visible-face counting, before the OCR backlog — because it
costs more than a count and far less than reading a page of text, and search
becomes more immediately useful once someone has enrolled a person than once
their photos are OCR-searchable. `nextPeopleRecord()` derives what still needs
scanning from `resolveMatch` itself rather than a separate dirty flag, so a
newly created profile, a changed profile, and a face-embedding-model version
bump all self-schedule through the same mechanism rather than three bespoke
invalidation passes.

Coverage distinguishes `not_started`, `partially_checked`, `complete`,
`paused`, `no_profiles`, `model_required`, and `profile_store_unavailable`.
`complete` is reachable only when every requested photo has a terminal answer
for every matchable profile — a retryable failure, an unresolved profile, or
an in-flight scan all keep it out of reach.

Shutdown, root revocation, pause, and disabling people search all abort the
in-flight scan and clear the coordinator's in-flight set, so a photo can never
be stuck reporting `checking` after the work that would have finished it was
cancelled.

## IPC and preload boundary

`services/people-ipc.ts` is the single point every People payload crosses.
Inbound values are parsed into narrow types or rejected — never cast. Outbound
values are projected field by field into `PeopleProfileView`,
`PeopleEnrolmentView`, and `PeopleFaceCandidateView` — never spread from an
internal type — so a field added to a stored shape later (an embedding, a
reference path) cannot reach the renderer by inheriting a spread; the compiler
would have to be told about it explicitly at the one place these views are
built.

The renderer does receive an opaque profile id, because Rename and Delete need
a handle and there is no safer one. Its inertness everywhere else is a tested
property rather than an assumption: the search-query contract rejects
UUID-shaped label entries outright, so an id offered as a `people_labels`
value fails validation instead of resolving; the Realtime tool schema has no
field shaped to accept one; and label-to-profile resolution happens in main
only, never from an id an outside caller supplies.

## Search integration

`hybrid-search.ts:applyPeopleFilter` is a hard pre-filter, exactly like the
existing face-count filter: it runs before semantic, text, or filename scoring,
and nothing downstream can admit a photo it excluded. Multiple named people use
AND semantics, with one asymmetry that matters: a **firm miss** on any
requested profile excludes the photo immediately (the answer cannot become yes
regardless of what the others say), while an **unresolved** profile excludes
it as coverage — because that is the one circumstance where the true answer
might still be yes. Ranking gives a likely match priority over a possible one
via a dedicated sort key ahead of every existing tie-breaker, so recency or a
filename match cannot push a weaker person-match result above a stronger one.

Filename and OCR-text matches never become biometric evidence: the people
filter reads only `peopleMatches`, and a photo named or containing the
requested label with no scanned match is excluded exactly as it would be for
anyone else. Semantic relevance still orders results within a tier — "may rank
but cannot manufacture" — because the concept score contributes to `fusedScore`
but never to filter admission when a person constraint is present.

## Realtime integration

`people_labels` joined the closed `search_documents` tool schema: at most
three names, 40 characters each. The renderer performs zero resolution — it
reads the array as plain strings and forwards it, exactly like every other
argument on that tool. Stored and spoken labels are untrusted text that can
reach a reason string (`"Likely match for Father"`) but nothing in the path
from tool call to displayed text ever evaluates a label as an instruction, a
template, or structured data. Labels shaped like a prompt-injection attempt —
`"ignore previous instructions"`, a JSON fragment, a tool-call-shaped string —
are proven inert two ways: shape-based ones are rejected outright by the
existing query contract (`shared/search-query.ts`, unchanged from Slices A-D),
and shape-legal ones are carried through as plain displayed text with no effect
on which photos are returned or how they are ranked.

## People settings and enrolment UI

`components/PeopleSettings.tsx` extends the existing intelligent-photo-search
settings card rather than adding a second surface. Enable/disable,
enable-triggered model download with a real progress bar, pause/resume,
per-profile Add reference / Rename / Rescan / Delete, and Delete all people
data are all present.

Enrolment reuses the app's only existing photo browser instead of building a
new one: while a draft is open, approved-folder search results in
`PhotoResultGrid` gain a "Use as reference for `<label>`" action, and choosing
one calls the same `addPeopleReference` IPC entry the rest of the enrolment
flow uses. "Add person" reveals a local, renderer-only label field first —
nothing crosses into main, and no draft exists anywhere, until that field is
submitted — which is what makes "dropping or selecting a file must not
automatically enrol it" true from the very first click rather than only once a
photo is involved. A multi-face reference stops and asks, with each candidate
rendered as an image plus a text caption; an unusable face states its reason in
both the caption and the accessible name, never in colour alone.

Adding one more reference to an **already-created** profile is a second,
lighter draft type (`person-enrollment.ts:beginAddition`): it reuses the exact
same add/select-face machinery and revalidation as a fresh enrolment, but needs
only one accepted reference rather than three, and — because it only ever
submits one reference — never runs the cross-reference consistency check that
compares a *submission* against itself.

The enrolment view is a nested section of the existing settings dialog and
inherits its focus trap rather than introducing a second one to keep
synchronized with the first.

## Deletion

Deleting one profile: removes its encrypted enrolment; removes its per-photo
`peopleMatches` entries from the index by compacting the journal, so the
profile id is gone from the bytes on disk rather than merely superseded by a
later line; cancels any of its queued scan work; and clears any in-progress
"add reference" draft scoped to it
(`person-enrollment.ts:cancelForProfile`) — a preview and a candidate id for a
person who is, in the same operation, about to no longer be enrolled.

Delete-all: clears the entire encrypted people directory in one recursive
removal; strips every Phase-3 field from every photo-index record while
leaving CLIP vectors, OCR text, and face counts untouched; clears every
in-memory enrolment draft; and turns people search off. Both single-profile
and delete-all are proven to survive a process restart — a fresh store or
coordinator instance over the same on-disk directory, not merely an in-memory
check — because "delete-all leaves no recoverable people data" is a claim
about disk state, not about the object that happened to receive the request.

## Real-model integration test

`real-people-inference.test.ts` runs the real installed models rather than
synthesized tensors, against procedurally generated pixel data — no
third-party or personal photograph is loaded from disk or committed. It is
skipped whenever the corresponding pack is absent, so the suite stays runnable
offline. Against YuNet (Phase 2's extras pack): the `kps_*` landmark tensors
exist at the shape the decoder assumes, and decoded points are finite and
score above the uncertain floor on structured noise. Against SFace, when the
people pack is installed: output is exactly 128 floats, the same input
produces the same output twice, normalizing to a unit vector holds, the same
input scores similarity 1 against itself and measurably lower against a
different input, and a landmark-driven alignment feeds a real embedding
without a shape mismatch. A test explicitly makes `fetch` throw and asserts
inference still completes, so "no network access" is demonstrated rather than
merely assumed from the absence of a call. No accuracy claim beyond these
structural properties is made from one synthetic-input suite.

## Security suite

Beyond the properties already under test throughout this document,
`people-security.test.ts` proves the boundaries between features hold as the
app grows: it reads `main/index.ts` and asserts that `personProfiles` and
`personEnrollment` are referenced nowhere outside the People IPC handler
block — not by capture, not by Telegram, not by dropped-file registration.
It reads the Realtime tool schema and `CompactSearchResult` and asserts
neither has a field shaped to carry a profile id, a similarity, or an
embedding. And it proves the people-pack download pipeline discards a digest
mismatch exactly like its CLIP and extras siblings, using the real pinned
manifest rather than a stand-in.

## Manual acceptance checklist

None of the following can be certified from automated checks. Prepared, not
claimed complete — a human pass on a real Windows install with real
photographs is required before Phase 3 can be called done in practice, not
only in code.

- create one profile from exactly three references
- multi-face reference photo: explicit face selection required, largest face
  never assumed
- a photo with no detectable face is rejected with a plain explanation
- a low-quality/too-small face is rejected with a plain explanation
- a clear likely match on a real photograph
- a clear possible match (partial angle, poorer lighting) on a real photograph
- a clear non-match on a real photograph
- a side-profile face
- a low-resolution face
- a group photo containing the labelled person among others
- a search naming two labelled people (AND semantics) on real photographs
- a semantic-concept query combined with a `people_labels` constraint
- an OCR `contains_text` query combined with a `people_labels` constraint
- an incomplete-scan state: search while some photos are still unchecked
- pause, resume, and a full application restart mid-scan
- rename a profile and confirm no rescan occurs
- add a reference to an existing profile and confirm only that profile rescans
- rescan a profile on demand
- delete one profile and confirm its matches disappear from search
- delete all people data and confirm nothing returns after a restart
- revoke an approved root mid-scan and mid-enrolment
- the installed Windows build (`Lumi Setup 0.1.0.exe`), not only the dev build
- CPU, RAM, and per-photo timing for SFace on a real photo library
- Phase 1/2 regression: semantic search, OCR, face counting all still work
- Telegram, voice, and screen-capture regression: unaffected by Phase 3
- keyboard-only walkthrough of the full enrolment flow
- a screen-reader walkthrough (NVDA or Narrator) of the People section
- Windows High Contrast and "Show animations" off

## Status

Code-complete: records, coordinator, IPC/preload, search and Realtime
integration, the People UI, deletion, the real-model integration test, and the
security suite are all implemented and verified. Reachable end to end from
Settings → People. The manual acceptance checklist above is the one thing
automated checks cannot certify. See docs/STATUS.md for exact counts.
