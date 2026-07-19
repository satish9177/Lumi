# Local intelligent photo search

Photos are embedded and searched **on this device**. No image, thumbnail,
embedding, or query vector is sent to OpenAI or anywhere else.

Status: the local-inference foundation is built and verified. The user-facing
search is not wired up yet — see *Not yet built* at the bottom.

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

## Not yet built

The following are designed for but not implemented, so there is no user-facing
semantic search yet:

- safe image decoding (header checks, 50 MP gate, `createThumbnailFromPath`)
- the indexing scheduler (pacing, pause/resume/cancel, power awareness)
- `concepts` on the search contract, and query-side prompt ensembling
- ranking fusion, honesty tiers, and reason labels
- trusted-result revalidation into the existing search-result store
- the settings card and reason badges
- updated Realtime capability instructions
