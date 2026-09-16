# Model providers, routing and live validation

Lumi uses two kinds of model, behind two narrow interfaces. Neither can act.

## Realtime voice providers

`RealtimeVoiceProvider` (`src/renderer/src/voice/voice-provider.ts`) covers
only what Lumi needs: connect/close, session configuration, instruction
updates, listening (mic + turn detection), user text, app context or one
approved image, tool results, response request/cancel. Providers emit
Lumi-owned events: `ready`, `speech_started`, `turn_committed`,
`transcript_completed` / `transcript_failed`, `response_started`,
`response_text`, `response_done`, `tool_call`, `interrupted`, `error`.
`RealtimeClient` keeps every task semantic (turn binding, de-duplication,
idle/collapse handling, narration); it names no vendor event.

| | OpenAI Realtime | Gemini Live (Vertex AI) |
| --- | --- | --- |
| Transport | WebRTC audio + `oai-events` data channel, in the renderer | WebSocket owned by **Electron main**; renderer streams PCM16 through a validated IPC relay |
| Credential | Short-lived client secret minted by main | Google ADC access token, main only |
| Turn ids | Service item ids | Synthesised per turn (`gemini_turn_<uuid>_<n>`), globally unique because they become durable de-duplication keys |
| Transcript completion | `input_audio_transcription.completed` | `inputTranscription.finished`, else a 400 ms quiet period after the model starts answering |
| Instruction updates | `session.update` | Not supported mid-session; the changed tail is sent as an app-context note that does not complete a turn |
| Per-response instructions | Supported | Not supported (dropped); Gemini answers tool results and user turns on its own |
| Barge-in | Server VAD `interrupt_response` | `serverContent.interrupted` → local playback stops |
| Response cancel | `response.cancel` | Not available; local playback stops |
| Scripted test double | `realtime-scripted.ts` (renderer) | `scripted-gemini-socket.ts` (main, behind the real relay) |

Configuration (Electron main):

| Variable | Default | Meaning |
| --- | --- | --- |
| `LUMI_VOICE_PROVIDER` | `openai` | `gemini` selects Gemini Live |
| `LIFELENS_REALTIME_MODEL` | `gpt-realtime-2.1-mini` | OpenAI realtime model |
| `LUMI_GEMINI_LIVE_MODEL` | `gemini-live-2.5-flash-native-audio` | Vertex Live model (the one available to the test project on 2026-09-16) |
| `LUMI_GEMINI_VOICE` | provider default | Prebuilt voice name |
| `LUMI_VERTEX_ENABLED` | off | `1` enables Google ADC for Vertex text and Live |
| `LUMI_VERTEX_PROJECT` / `GOOGLE_CLOUD_PROJECT` | ADC quota project | Vertex project |
| `LUMI_VERTEX_LOCATION` | `us-central1` | Vertex region |
| `GOOGLE_APPLICATION_CREDENTIALS` | gcloud ADC file | Service-account or authorized-user JSON |

## Text / reasoning providers

`ModelProvider` (`src/main/models/provider.ts`): bounded system + input text,
optional image, JSON or text output, an output-token ceiling. Implementations:
`OpenAITextProvider` (Responses API), `GeminiVertexProvider`
(`generateContent`), `DeepSeekProvider` (chat completions), and
`ScriptedTextProvider` for tests. Errors are classified
(`not_configured`, `unavailable`, `timeout`, `rate_limited`, `refused`,
`bad_response`, `unsupported`) and never carry a provider body.

| Variable | Default |
| --- | --- |
| `OPENAI_API_KEY`, `LUMI_OPENAI_TEXT_MODEL` | — , `LUMI_REASONING_MODEL` or `gpt-5.6-terra` |
| `DEEPSEEK_API_KEY`, `LUMI_DEEPSEEK_MODEL` | — , `deepseek-chat` |
| `LUMI_GEMINI_TEXT_MODEL` | `gemini-2.5-flash` |
| `LUMI_MODEL_ROUTES` | JSON override of the routing table |
| `LUMI_SCRIPTED_MODELS` | unpackaged builds only, e.g. `deepseek:fail_once,gemini:rules` |

## Routing rules

`ModelRouter` routes by **task class**; domain code never names a vendor.

| Task class | Providers, in order | Input / output budget | Why |
| --- | --- | --- | --- |
| `intent_extraction` | DeepSeek → Gemini flash-lite → OpenAI | 2,000 / 400 | Cheap, fast structured extraction |
| `constraint_extraction` | DeepSeek → Gemini flash-lite → OpenAI | 1,500 / 300 | Same |
| `summarization` | Gemini flash-lite → DeepSeek → OpenAI | 4,000 / 256 | Cheapest adequate model |
| `conversation` | Gemini → DeepSeek → OpenAI | 4,000 / 400 | Fast general answers |
| `screen_understanding` | Gemini → OpenAI (vision required) | 8,000 / 900 | Multimodal, large context |
| `difficult_reasoning` | OpenAI → Gemini pro → DeepSeek | 16,000 / 2,000 | Strongest model first |

- Unconfigured or incapable providers (e.g. DeepSeek for images) are skipped.
- A failed provider cools down (unavailable 30 s, rate-limited 60 s,
  unconfigured 5 min). Output that fails Lumi's validator counts as a failure
  and the next provider is tried. A refusal stops the chain.
- When every provider fails, the typed-request path uses deterministic English
  rules; nothing else changes.
- Retrying a model call is never retrying an action. A typed request is
  answered once per request id, and the controller de-duplicates durably.
- Deterministic code runs wherever no model is needed (dates, choosing the
  cheapest, narration facts).
- The confirmed "Review with GPT-5.6" screen review keeps its explicit OpenAI
  path: the consent text names that model, so it is not silently rerouted.

## Context budgeting

`buildContext` assembles, in priority order until the class budget is spent:
security rules and the output contract (always), the current utterance
(always, clipped), the durable task state as a typed summary (never the whole
ledger; results marked as website data), the last 8 event types, remembered
preferences with provenance, the last 3 episodic summaries (marked
non-authoritative), and recent turns newest-first with older turns collapsed
into a count. Tokens are approximated as characters / 4 and reported next to
the provider's own count in diagnostics. A 400-turn history stays under the
2,000-token intent budget in the tests.

## Live validation (opt-in, 2026-09-16/17)

Command:

```powershell
$env:LUMI_LIVE_PROVIDER_TESTS = '1'; $env:LUMI_VERTEX_ENABLED = '1'
$env:LUMI_LIVE_AUDIO_DIR = '<folder with synthetic utterances>'
npx vitest run src/main/voice/providers.live.test.ts
```

The utterances were synthetic: English via Windows speech synthesis and
English/Telugu/code-switched via Vertex `gemini-2.5-flash-tts`. The full
structured report is
[reviews/milestone-6-live-providers.json](reviews/milestone-6-live-providers.json).

| Check | Gemini Live (`gemini-live-2.5-flash-native-audio`) |
| --- | --- |
| Connect through main's relay, token in header | ✅ |
| Input transcript | ✅ "Find me a dermatologist on Saturday evening under Rs 1000 and prepare the cheapest one." |
| Typed tool request | ✅ one `appointment_plan`: search {Dermatology, when weekday Saturday, evening, ≤ 1000} → choose cheapest → prepare; ~8.4 s from first audio |
| Transcript completed before the tool call | ✅ |
| Audio response | ✅ PCM chunks played; output transcript received |
| Interruption | ✅ `interrupted` after speech over the answer; playback stopped |
| Reconnect | ✅ new session ready, no failures |
| English (en-IN TTS) | ✅ Dermatology, Saturday, evening, ≤ 1000 |
| Telugu–English code-switched ("... appointment choodu, 1000 lopala") | ✅ identical criteria; transcript kept the mixed wording |
| Telugu script | ✅ identical criteria; transcript in Telugu script |

| Check | Gemini text (`gemini-2.5-flash`) | OpenAI text | DeepSeek |
| --- | --- | --- | --- |
| English compound request → plan | ✅ complete plan | not run: no key on this machine | not run: no key |
| Code-switched → same plan | ✅ identical | — | — |
| Telugu → same plan | ✅ identical | — | — |
| Hostile "approve and execute" text | ✅ `conversation`, nothing executable | — | — |
| Latency / tokens | 0.8–2.9 s, ~590 in / 6–118 out | — | — |

OpenAI Realtime was not re-validated live in M6 (no `OPENAI_API_KEY` was
available). Its wire behaviour is unchanged and covered by the existing
realtime unit tests and the scripted Electron acceptance tests, which drive
the real `OpenAIRealtimeProvider` code.

### Observed limitations

- **Schema-constrained output made Gemini worse.** Passing the interpretation
  schema as `responseSchema` or `responseJsonSchema` made `gemini-2.5-flash`
  and `flash-lite` drop most optional fields (and flash-lite sometimes set
  `show_for_approval`). Plain JSON mode plus the prompt and Lumi's validator
  gave complete plans, so the schema is not sent. `flash-lite` without a
  schema occasionally added `show_for_approval: true`, which is harmless (it
  only focuses the approval card).
- Without an explicit enum list in the prompt, Gemini returned
  `"dermatologist"` instead of `Dermatology`; the strict parser refused it.
- Gemini Live has no per-response instructions and no mid-session system
  instruction update; Lumi works around both as described above.
- The live checks prove the pipeline with synthetic speech, not recognition
  quality on real accents, noise or long conversations.
