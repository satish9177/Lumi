# Local intelligent photo search

Photos are embedded and searched **on this device**. No image, thumbnail,
embedding, or query vector is sent to OpenAI or anywhere else.

Status: the Phase 1 local-inference and user-facing semantic-photo-search path
are built. OCR, face identity, people counting, video, RAW/HEIC, and cloud
folder analysis remain deliberately out of scope.

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

Records hold only local metadata: image id, root id, root-relative path, name,
mtime, size, dimensions, vector row, model version, status, a bounded failure
code, attempts, updated time. **No absolute paths, image bytes, thumbnails, OCR,
face data, or OpenAI data.** Absolute paths stay main-owned and are rebuilt from
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

# Phase 3 — user-labelled people (foundation, integration pending)

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

## Status

Slices A-D and the query contract are implemented and verified. The per-photo
match records, coordinator scheduling and People UI are not yet built, and
nothing is wired into the running application. See docs/STATUS.md.
