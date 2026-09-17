# Lumi security model

**Models suggest; the durable controller acts.** Nothing a model says —
realtime, text, or webpage text a model repeats — can approve, execute, choose
a URL, or name a DOM element.

## Trust boundaries

| Boundary | Rule | Enforced by |
| --- | --- | --- |
| Renderer → main | Fixed channels, positional primitives, sender/frame check, every payload re-parsed in main | `ipc-sender.ts`, `agent-ipc.ts`, `preload/index.ts`, boundary tests |
| Main → runtime | Allowlisted routes only, per-process bearer credential, loopback, no redirects | `agent-runtime-supervisor.ts` |
| Runtime → worker | Per-worker token, closed operation registry, allowlisted origins by *site name*, typed input | `app/browser/*` |
| Webpage → Lumi | Page text is data: typed fields only, bounded and shape-checked; free text is spoken only if it looks like a plain value | adapter, `agent-wire.ts`, `narratableText` |
| Model → controller | Closed vocabulary (enums, bounded integers, `HH:MM`, relative-day kinds, short names); unknown keys refused | `plan-wire.ts`, `parseVoiceTaskCommand`, `parseInterpretation` |
| Speech → approval | No approve/execute tool, command, IPC method or route is reachable from voice or text | `VoiceTaskBackend` type omits them; tests assert they are never touched |

## Credentials

| Secret | Lives in | Never reaches |
| --- | --- | --- |
| `OPENAI_API_KEY` | Electron main environment | Renderer (it gets a short-lived realtime client secret only), runtime, worker |
| `DEEPSEEK_API_KEY` | Electron main environment | Everything else |
| Google ADC / service account and access tokens | Electron main (`google-auth.ts`), used for Vertex text calls and the Gemini Live socket | Renderer (Gemini audio is relayed through main), runtime, worker, logs |
| Runtime bearer token | Minted per process by main | Renderer, logs, disk |
| Worker token | Minted per worker by the runtime | Main, renderer, database, logs |
| Database URL | Runtime environment; packaged app reads it from `%APPDATA%\Lumi\agent-runtime.json` in main | Renderer, installer, logs |

The Google token endpoint is fixed in code; a credential file cannot redirect
the exchange. Provider errors are classified into codes and never carry a
provider response body. The packaged runtime bundle is checked for `.env`
files at build time and in the packaged acceptance test.

## Gemini Live relay

A browser WebSocket cannot send an `Authorization` header, and a Google token
must not reach the renderer, so Electron main owns the Vertex socket. The
renderer may send only: `setup` (instructions + tool declarations, size-bounded
and name-checked; main chooses model, project, region and voice), `audio`
(base64 PCM, bounded), `audio_end`, `text`, `image` (PNG/JPEG, bounded) and
`tool_response` (known call id and tool name). Server frames are projected
into a closed event set; thought parts, unknown frames and oversized payloads
are dropped. `harness_say` exists only in unpackaged scripted builds.

## Model output

- Realtime tool calls and text-model JSON go through the same strict parser.
  A malformed or hostile answer is treated as that provider failing; the
  router may try another provider, which is safe because model calls have no
  side effects.
- Plans run at most four steps in a fixed order and always stop before
  approval. "Book it" only surfaces the trusted card.
- Relative dates are resolved by code from a relative-day *kind*; the model
  never invents a calendar date. Ambiguity ("next Saturday" on a weekday) is
  asked back.
- Refusals are not shopped to a more permissive provider.

## Memory

Preferences come only from an explicit user statement, carry provenance, fill
only constraints the current request left open, and never touch booked
values. Episodic summaries are app-authored, sanitised, and labelled as
non-authoritative in model context. The website — read now, by the worker —
is the only authority for prices, times and availability.

## Observability without leakage

Diagnostics are built from a closed field set (ids, codes, counts). Values
that are not code-shaped, or look like credentials, are dropped. No prompt,
transcript, form value or header is recorded. Diagnostics are visible only in
development builds or with `LUMI_DIAGNOSTICS=1`.

## Packaged runtime

The runtime, its interpreter, dependencies and Chromium ship inside the
application's `resources` directory and are byte-compiled at build time; at
run time `PYTHONDONTWRITEBYTECODE=1` keeps the runtime from writing beside its
own code (the packaged acceptance test checks this). Main starts only fixed
modules with fixed arguments and no shell; the renderer has no path, command
or code channel to them. A per-user install directory is writable by that
Windows user, as for any per-user application; an all-users install under
Program Files is not.
The only user-editable input is `agent-runtime.json`, read and validated by
main. Distribution builds must be Authenticode-signed; see
[PACKAGING.md](PACKAGING.md#windows-application-control).

## Public page inspection (Milestone 7a)

Page inspection is disabled unless trusted configuration names public hosts.
The destination policy is enforced by main, the runtime and the worker, and the
worker's network guard applies it to every request and redirect hop. Page
content is `untrusted_environment` data: it reaches a model only inside a
delimited section, only for providers the user's approval named, and an answer
is stored only if its quotes and numbers are verifiably on the page. Details
and limits: [PAGE-INSPECTION.md](PAGE-INSPECTION.md).

## Known gaps

- The packaged build is unsigned. Machines with Smart App Control or WDAC in
  enforcement mode can block unsigned binaries (observed; see PACKAGING.md).
- Voice narration is steered, not enforced: a live model could still misspeak.
  The card and timeline remain the authority.
