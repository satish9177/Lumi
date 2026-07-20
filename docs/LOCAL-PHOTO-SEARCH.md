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
