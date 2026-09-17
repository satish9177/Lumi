# Lumi: incremental general computer use

Architecture review and proposed next milestones — 17 September 2026.

**Recommendation:** retain Electron main as the credential and desktop boundary, Python/PostgreSQL as the execution authority, and isolated workers. Add a small, closed registry of generic operations with scoped authorization, versioned observations, and verified effects. Start with public web research. Add authenticated interaction, Windows accessibility, and cross-app work only after their respective safety gates pass.

This is a design document, not implementation. Repository inspected at `e68dcd3`; working tree was clean before this document. Code and selected tests were inspected; suites were not rerun. Historical test results below are attributed to the repository report, not claimed as fresh verification. Proposed file names are marked “new.”

## A. Current-state assessment

Lumi is a durable, domain-constrained desktop agent with additional companion tools. It is not currently a general browser controller or a desktop automation engine. The inability to inspect a LeetCode profile follows from several intentional boundaries, not merely from an unhelpful system prompt.

| Evidence in the repository | What it establishes | Reuse / change |
|---|---|---|
| `src/main/agent/task-request-interpreter.ts`, `src/shared/plan-wire.ts` | The interpreter recognizes appointment, clinic, status, reconciliation, cancellation and preference intents. Plans exclude URLs, selectors and scripts. | Keep existing intents intact; add a separate computer-task intent and parser. |
| `src/main/services/voice-task-controller.ts` | Main coordinates a bounded domain plan; speech can surface an approval card but cannot approve. | Reuse turn provenance and duplicate protection; do not turn this booking controller into a giant generic executor. |
| `services/agent/app/browser/registry.py` | Six reviewed operations, all from the fixture adapter; effect/retry/reconciliation declarations are already first-class. | Extend this design with reviewed generic primitives. A closed operation vocabulary does not require closed website coverage. |
| `services/agent/app/browser/worker.py`, `session.py` | A fresh browser context and page are created and closed for each dispatch. Worker deduplication is in memory and generation-bound. | Preserve dispatch fencing; introduce explicit session and tab ownership for general browsing. |
| `services/agent/app/services/browser_execution.py` | Execution/reconciliation dispatch is explicitly `commit_booking` / `lookup_booking`; immutable proposals and action-derived references matter. | Extract a small execution descriptor seam; retain the booking implementation as the strongest reference implementation. |
| `services/agent/app/services/actions.py`, `domain/action_status.py`, `db/tables.py`, migration `0002` | Immutable proposals; digest/revision-bound approvals; atomic approval claim and attempt creation; one approval per attempt. `requires_approval()` currently returns true for every tier. | Reuse these invariants. Scoped permissions need an explicit schema extension, not a risk-label shortcut. |
| `services/agent/app/services/observation.py` | Discovery reads have no action/attempt and are not stored in the per-action dispatch ledger; their task-level results are recorded elsewhere. | Add durable observation metadata and computer-step records. Do not claim all current reads already have an attempt ledger. |
| `services/agent/app/services/recovery.py`, `domain/sites.py` | Interrupted attempts become unknown; absent results prove failure only under a reviewed authority declaration. | Reuse almost directly; generalize reconciliation evidence. |
| `src/main/services/agent-ipc.ts`, `agent-runtime-supervisor.ts`, `src/preload/index.ts` | Fixed IPC, main-side validation, allowlisted runtime routes, process credentials and supervision. | Extend explicitly; never add renderer `executeTool(name, json)` or generic IPC. |
| `src/main/models/{model-router,context-builder,model-config}.ts` | Provider routing, bounded context and model-call-only failover are implemented in main. | Add computer-planning context and strict hard caps. Keep keys out of Python and renderer. |
| `src/main/services/{capture,document-search,tools}.ts`, `src/features/document-tools/search.ts` | User-initiated captures, approved-root search, revalidated file IDs and separate confirmation tools exist. General document content reading is explicitly unsupported in `tools.ts`. | Reuse root/file identity and capture UX. Resume comparison needs a new bounded document parser; it is not already available. |
| `scripts/build-agent-runtime.mjs`, `src/main/agent/packaged-runtime.ts` | Python, production dependencies and Chromium are bundled and supervised. | Extend existing packaging; no new service platform. |

Important qualifications:

1. **The controller is split today.** Main orchestrates intents and model calls; Python owns durable state, approval claims and browser execution. Preserve this division with a clearer proposal/execute contract rather than moving all planning or credentials into Python.
2. **Process separation is not a full OS sandbox.** A Python worker running as the user can theoretically access user files. Its narrow API and stripped environment constrain ordinary execution; they do not defeat arbitrary native code execution in that process.
3. **“Exactly once” is too broad as a web-wide promise.** Lumi can prevent automatic duplicate dispatch of an approved operation and refuse uncertain retries. It cannot atomically commit both its database and an arbitrary website. Recovery may remain uncertain indefinitely.
4. **Current browser safety relies on reviewed code and a controlled site.** Checking a dispatch’s `submitted` flag is not a network-level proof that arbitrary navigation had no effects.
5. **Historical validation is substantial but not universally green.** `docs/reviews/milestone-6.md` reports 428 Python tests passing, 43/43 deterministic evals, desktop/package checks, and known JavaScript baseline failures. OpenAI and DeepSeek live checks were skipped. Do not relabel these as fresh or complete provider validation.
6. **Some documentation is stale.** `services/agent/README.md` still contains an earlier statement that packaged builds do not bundle Python, contradicted by current packaging code and the later review.

## B. Target architecture

```text
User voice / text                         Trusted Lumi controls
        |                                Start scope / approve effect / stop
        v                                            |
Renderer + fixed window.lifeLens methods             |
        | validated IPC, trusted sender              |
        v                                            v
ELECTRON MAIN — credentials and desktop boundary
  Intent routing ── bounded ComputerPlanner ── model router
       |                   ^                      |
       | proposals         | filtered observations| provider keys stay here
       v                   |                      v
  Typed runtime client     |               Text / vision / voice providers
       |                   |
       v                   |
PYTHON DURABLE CONTROLLER ──┘
  Validate proposal → evaluate policy → bind authorization
  Persist immutable action + attempt + dispatch before execution
  Verify evidence → commit result → expose next task snapshot
       |
       +── PostgreSQL: existing tasks/actions/approvals/attempts/events
       |                 + grants, observations, sessions, step authorizations
       |
       +── Browser executor → isolated Playwright worker
       |      ├── existing reviewed clinic adapter
       |      └── generic primitives → owned Chromium sessions/tabs
       |             public profile / authenticated profile (separate)
       |
       +── Desktop executor → isolated Python UIA helper [later]
       |      UI Automation / limited Win32 input; no shell or provider keys
       |
       └── Main-effect queue → ELECTRON MAIN brokers [later]
              approved-root files / capture / approved app launch / run recipe
              claim generation-bound dispatch → revalidate → act → report

All outputs → observation/evidence projection → verification → durable result
Unknown effect → read-only reconciliation → known result OR remains unknown
```

Main remains the driver of model calls: it fetches a fresh durable snapshot, asks for one proposal, and submits it with the expected task revision. Python is the only authority that turns a proposal into executable work. A restarted main reads state; it does not replay a stored model transcript.

For later main-owned file/app operations, main claims a narrowly typed pending dispatch over the existing authenticated runtime channel and reports its result. No reverse general-purpose runtime-to-main RPC or new unauthenticated listener. Give these dispatches main’s process generation and the same ambiguity rules as browser dispatches.

Keep one active task, one operation in flight and one desktop input owner initially. Multiple browser tabs are resources inside that task, not concurrent agents.

## C. Key design decisions

| Decision | Options considered | Recommendation and reason | Tradeoff |
|---|---|---|---|
| Browser engine | Replace Playwright; raw CDP; current Playwright | Retain Python Playwright and its existing worker lifecycle. Use internal CDP only for a missing observation capability. | Engine/version behavior still needs packaged tests. |
| Browser session | Fresh context each step; normal user profile; dedicated profiles | Task-persistent public context, then a separate managed authenticated profile. | User signs in again in Lumi’s profile. |
| Abstraction | Domain adapters only; raw click/JS; bounded semantic primitives | Generic navigation/observation/interaction with worker-issued target references; reviewed adapters remain preferred for supported commitments. | Unknown controls may require approval or handoff. |
| Planner | Model directly drives worker; multi-agent planner/critic; single bounded planner | One planner proposes one next action, deterministic policy and verification. Escalate one difficult reasoning call only when justified. | Less speculative parallelism, much clearer recovery. |
| Authorization | Per-click confirmation; blanket “allow computer”; scoped grants plus exact approvals | Trusted task-scope grant for bounded browsing; single-use exact approvals for consequential effects. | Requires a real authorization migration and clearer UI. |
| Observation | Full screenshots; raw DOM; filtered semantic observations | Browser DOM/ARIA or Windows UIA first, region vision only when needed. | Some canvas and inaccessible apps need a later fallback. |
| Windows backend | PyAutoGUI; C# sidecar; Python UIA | Small isolated Python helper using `pywinauto` UIA initially, behind a backend interface. | Must validate COM behavior, Python compatibility and application coverage; C# remains a fallback, not a second first implementation. |
| Project execution | Visible-terminal typing; arbitrary command strings; approved recipes | User-confirmed structured run recipes in a supervised process. | Recipes execute repository code and cannot honestly be called safe solely because `shell=false`. |
| Generic submission | Allow any approved button; forbid all effects; verified effect descriptors | Ship generic preparation first. Execute commitments only when effect, target, verifier and reconciliation policy are supportable. | Broad preparation arrives sooner than broad autonomous submission. |
| State storage | New orchestration framework; parallel action ledger; extend current ledger | Add small tables/fields to PostgreSQL and reuse action invariants. | Migrations and contract updates are unavoidable. |

These recommendations deliberately limit broad claims: arbitrary interactive websites cannot simultaneously provide perfect read-only semantics, universal functionality, and authoritative recovery. Lumi should expose the limit instead of hiding it behind an LLM risk score.

## D. Computer-use tool API

The public model vocabulary should have a few typed families. A discriminated union must reject unknown keys and invalid field combinations. The following is an interface specification, not implementation code.

| Family | Typed variants | Restrictions |
|---|---|---|
| `observe` | `surfaces`, `document`, `elements`, `region`, `wait` | Surface/window scope, bounded projection, typed wait predicate. `document` includes text and links; no separate overlapping `read_page` / `get_links` APIs. |
| `browse` | `navigate`, `search`, `back`, `forward` | URL or observed link reference; search query is an approved public query; HTTPS/public destinations by default. |
| `tab` | `create`, `activate`, `close` | Only task-owned tabs; closing dirty tabs pauses. User tabs are not implicit task resources. |
| `interact` | `invoke`, `set_value`, `select`, `scroll`, `key`, `drag` | Element reference and fresh observation; finite key vocabulary; coordinate target only under the later fallback policy. No free selectors or scripts. |
| `app` | `launch`, `focus` | Registered app ID, validated document/workspace ID; no arbitrary executable path or URI arguments. |
| `file` | `list`, `search`, `read`, `open`, `create`, `copy`, `move`, `rename`, `trash` | Root and file references; bounded content; destination root plus validated relative name. No general absolute-path tool. |
| `transfer` | `download`, `upload` | Explicit source and destination, disclosure manifest, byte/type limits. Upload is always an effect proposal. |
| `project` | `start`, `status`, `stop` | User-registered recipe ID, pinned revision, project ID and run ID. Never a command string. |

Not all families ship together. Navigation to a user-supplied address is not arbitrary network authority. A URL has provenance and passes policy. An observed link is still untrusted, even though the worker issued its reference.

`search` builds an encoded URL for a configured search provider, or later uses a reviewed provider API. It does not accept an arbitrary fetch endpoint, headers or a request body. Prefer native site search when a supported interaction scope exists; otherwise public web search is sufficient for GitHub/job discovery.

### Proposed records

| Record | Required fields / types |
|---|---|
| **ModelProposal** | `schemaVersion: 1`; `basedOnTaskRevision: integer`; `observationId: UUID`; `operation: discriminated Operation`; `purpose: bounded string`; `suggestedPostcondition: typed Predicate`; `evidenceRefs: bounded EvidenceRef[]`. No approval, grant creation, risk override, retries, executable code or credentials. |
| **ActionEnvelope** | `actionId`, `taskId`, `stepId`, `effectGroupId`: controller UUIDs; `taskRevision`; `operation`; `target: TargetRef`; `parameters`: operation-specific object; `observationRef`; `preconditions: Predicate[]`; `verification: VerificationSpec`; `effect: EffectSpec`; `authorizationRequirement`; `retry: RetrySpec`; `reconciliation: ReconciliationSpec`; `timeoutMs`; `policyVersion`; `schemaVersion`; `proposalDigest`; `evidenceRefs`. Controller constructs all authority-bearing fields. |
| **TargetRef** | Union: browser `{sessionId, tabId, frameId, documentEpoch, elementRef?}`; desktop `{workerGeneration, windowId, processId, processStartTime, elementRef?}`; file `{rootId, fileId, expectedVersion}`; app `{appId}`; project `{projectId, recipeId, recipeRevision}`. Coordinate variant adds `{captureId, windowId, clientPoint, bounds, dpi, expiresAt}`. |
| **Observation** | `{id, taskId, sessionId?, workerGeneration, surfaceRef, sequence, documentEpoch?, capturedAt, contentHash, projection, truncated, evidenceRefs, sensitivity, provenance: "untrusted_environment"}`. Element maps and raw evidence stay local; model projection carries opaque references and bounded labels. |
| **EffectSpec** | `{class: local_read \| public_navigation \| local_change \| disclosure \| external_mutation \| destructive \| security_sensitive \| unknown, riskTier: R0..R3, dataSources, recipients, accountRef?, effectKey, mayAutosave, classificationReason}`. No model-selected low-risk exemption. |
| **VerificationSpec** | `{kind: navigation \| fields \| file_manifest \| process_health \| authoritative_receipt \| reviewed_adapter, expectedFacts, authorityRef?, timeoutMs}`. No executable predicate; the controller validates that the predicate is appropriate for the effect. |
| **RetrySpec** | `{mode: repeat_read \| observe_then_repropose \| reconcile_only \| never_auto, maxAttempts, backoffMs}`. Derived from operation and scope; retries cannot outlive authorization. |
| **ReconciliationSpec** | `{strategyId, version, correlationRef, authorityRef?, absenceRule: authoritative \| not_authoritative, deadlineMs}`. Unknown generic effects do not acquire an invented strategy. `manual_review` can record evidence but cannot itself prove absence. |
| **TaskGrant** | `{id, taskId, userIntentRef, revision, confirmedAt, expiresAt, allowedOperations, surfaceScope, originScope, rootPermissions, allowedDataRefs, allowedRecipients, captureScope?, maxSteps, budget, revokeEpoch}`. Persisted after a trusted renderer click. |
| **StepAuthorization** | `{id, grantId, grantRevision, actionId, actionRevision, digest, policyVersion, expiresAt, consumedByAttemptId?}`. Runtime-created only after checking the action is inside the confirmed grant. Single-use, despite the grant being reusable. |
| **Dispatch** | Existing dispatch/attempt/runtime/worker identifiers plus `{executorKind, sessionRef?, actionDigest, authorizationEpoch, deadline, effectGroupId}`. Worker revalidates target/scope and generation. It cannot mint authority. |
| **ActionResult** | `{status: verified \| no_effect \| outcome_unknown, observationRef, evidenceRefs, verifierId, verifierVersion, failureCode?, effectMayHaveOccurred}`. Transport completion is not task completion. |

Opaque references resolve only inside their task/session and generation. The model cannot mint an element ID by printing a plausible string. Navigation, frame replacement, account changes and process restart invalidate relevant references. Immediately before acting, revalidate element role, frame, label, visibility, enabled state and any effect-critical values. No `force` click.

Keep a versioned canonical serialization. The immutable digest includes target identity, exact disclosed values or protected value references with hashes, recipient/account, effect descriptor, expected state and policy version. Never include access tokens or cookies. New observations cannot silently change an approved payload.

### Minimal persistence evolution

The first PR can retain exact single-use approvals. For reusable scope in the next slice:

* Add `task_grants`, `step_authorizations`, `observations` and browser-session metadata. Store filtered facts/references, not full DOM or secrets.
* Extend attempts with nullable `step_authorization_id`; enforce **exactly one** of `approval_id` or `step_authorization_id`. Preserve unique consumption and one unfinished attempt per action. Existing approvals retain their original constraints.
* Add `AUTHORIZED` for the grant branch, with a distinct `action.authorized` event. Do not fabricate `action.approved` or claim that the user reviewed every click. Existing `APPROVED` and booking transitions stay intact.
* Extend the dispatch table for session and scope references, preserving one dispatch per attempt. Retain the physical browser table initially; generalize/rename it and its worker-generation FK when adding desktop execution, rather than introducing a second action ledger.
* Record action-free observation calls in observation records and task events. Any generic operation that navigates or changes UI state has a durable action.
* Preserve unresolved `effectGroupId` locks across task cancellation/restart. A fresh action or new task must not bypass an uncertain commitment. Initially block all new consequential work while any such action is unresolved; loosen only with a tested resource-conflict model.

## E. Execution loop and state machine

Task phase and action status are distinct. An action succeeding does not mean the task succeeded.

```text
Task: CREATED → PLANNING ↔ READY → EXECUTING → VERIFYING → PLANNING
                      |              |             |
                      v              v             +→ SUCCEEDED (goal evidence)
              WAITING_APPROVAL   OUTCOME_UNKNOWN
                                      |
                                      v
                                 RECONCILING
                              /        |        \
                         verified   unknown    authoritative no-effect
                            |          |                 |
                       PLANNING      PAUSED          PLANNING

Exact action: PROPOSED → WAITING_APPROVAL → APPROVED → EXECUTING
Grant action: PROPOSED → AUTHORIZED [new]             → EXECUTING
EXECUTING → SUCCEEDED | FAILED (known no-effect) | OUTCOME_UNKNOWN
OUTCOME_UNKNOWN → RECONCILING → SUCCEEDED | FAILED | OUTCOME_UNKNOWN
```

The task can be paused while its unresolved action remains `OUTCOME_UNKNOWN`. Use existing `PAUSED` with a reason such as `login_required`, `stuck`, `budget`, `user_takeover` or `unsupported_effect`; avoid adding a dozen task states.

Exact algorithm:

1. Load the durable task, unresolved effects, grant revision, budget and current process generations. If an effect is unknown, take only the reconciliation path. A cancelled task never resumes planning, though its effects remain reconcilable.
2. Obtain a scoped observation. Passive local observation requires existing scope; navigation/capture/disclosure requires the corresponding renderer confirmation or valid grant. Filter and label environment content before any provider call.
3. Assemble trusted objective, remaining capabilities, unresolved constraints and current evidence. The model returns one next operation or a proposed answer/clarification. A short plan of at most four subgoals is advisory only.
4. Strictly parse output. Check task revision, target provenance, data-flow scope, deterministic effect policy, budget and unresolved-effect conflicts. Missing/stale/ambiguous facts cause another observation or pause, not guessing.
5. Create the immutable action. If no matching grant or if the effect requires exact approval, display a controller-authored approval card and stop dispatch. Spoken “yes” only focuses that card.
6. At execution claim, atomically check current task/grant/approval revisions and expiry, consume the single-use authorization, persist attempt and dispatch intent, and advance the task. Commit before contacting an executor. No transaction remains open during automation.
7. Worker checks generation, digest, scope, target freshness and deadline. Record locally that an effect **may begin before issuing any input**, including focus/typing when autosave is possible. For generic interaction, a booking-specific `submitted` flag is insufficient.
8. Execute one bounded operation, observe, and run its deterministic verifier. Write dispatch evidence and finish the attempt. Lost replies, ambiguous failures or post-effect verification failures become unknown, not ordinary failure.
9. Update a compact factual progress summary. Continue only after a known result; re-plan after every interaction, unexpected navigation, target invalidation, manual takeover or verification mismatch.
10. Finish only when the goal’s required evidence exists. Answers cite page/file observations; “not found,” “not publicly visible” and “could not verify” remain valid outcomes.

Initial default budgets, persisted per task: **20 executed steps, 30 observations, 20 planner calls, 5 task-owned tabs, 5 minutes active execution, 60,000 input tokens and 8,000 output tokens across all model attempts, at most 2 image calls**. These are proposed tuning values, not measured requirements. Approval waiting pauses active-time accounting but not grant expiry. Use a ten-minute grant expiry initially; extending any budget requires a trusted UI continuation.

Two identical failed proposals at the same material state trigger a different approach; three non-progressing steps or a detected A→B→A loop pause. Cap model failover at two providers per proposal within the total budget; refusal/policy denials do not trigger permissiveness shopping. Unknown effects are never “stuck-loop retries.” Pure reads may retry twice with backoff while authorization remains valid.

## F. Security model

### Threat model

| Threat | Enforced boundary | Residual limit |
|---|---|---|
| Website/document tells the model to upload files or reveal keys | Untrusted observation channel; filesystem IDs only from approved roots; disclosure manifests; destination-bound capabilities; no model authority fields | A model can still misunderstand content. Policy must stop the proposed effect even when the model is persuaded. |
| Malicious page hides instructions in ARIA, OCR, image, iframe or downloaded PDF | All observation sources carry the same untrusted provenance, including summaries; bounded parser, no executable content | Sanitization cannot make arbitrary prose trustworthy. |
| Page makes a benign-looking button destructive | Unknown activation is not classified read-only from its label; fail closed or require a supported exact effect | DOM cannot prove what a remote server will do. Generic consequential execution is intentionally incomplete. |
| Exfiltration through URL query, search, form field, upload or image request | Public research has no local/private source capability; query/URL provenance checks; separate public/authenticated contexts; explicit data and recipient scope | Once a third-party page legitimately receives sensitive data, Lumi cannot control that server or all of its scripts. |
| Approval spoofing / injected “approve” text | Approval cards built from controller facts in Lumi, never webpage HTML; no browser/desktop action may target Lumi/preload/DevTools | Same-user malware or a compromised trusted main process is outside this boundary. |
| Stale element or wrong window receives input | Epoch-bound refs; focus/foreground/geometry checks; no forced click; manual input pauses | Very small UI races remain; uncertain effects must be treated conservatively. |
| Duplicate effect via crash, double-click, alternate tool or new task | Atomic claims, unique attempt/dispatch, worker generation, effect-group lock, unresolved-task gate | No remote idempotency unless the remote system actually supports it. |
| Website reaches loopback/runtime/cloud metadata | Token authentication plus destination policy for top-level and subresource traffic, redirects and downloads | URL string validation alone does not stop DNS rebinding or every browser transport. |
| Compromised automation worker | Narrow protocol, no provider/database credentials, Chromium sandbox, stripped environment, parent lifecycle controls | Python process isolation is not OS privilege isolation. Strong containment would require an additional restricted-token/container design. |
| Tool output leaks secrets through diagnostics | Closed diagnostic fields; protected local evidence store; redaction before model/log projection | Redaction is imperfect; exclude credential surfaces entirely. |

Policy checks must also apply to model-generated URLs and text. “The user asked for research” cannot authorize appending a resume, private email or task history to a search query. For sensitive workflows, keep a task-level data classification even after summarization; model paraphrasing does not declassify data.

**Network design:** public browsing starts with HTTPS, normal public destinations, no URL credentials, no local file/device/custom schemes, and no automatic downloads/protocol handlers. Validate every redirect and frame destination. Block service workers initially, deny unexpected popups, browser permissions and certificate-error bypasses. Intercept WebSocket connections and background requests as part of policy, not only page navigation. Playwright documents service-worker interception limitations; request interception alone is not an egress sandbox. [Playwright network documentation](https://playwright.dev/python/docs/network), [service-worker limitations](https://playwright.dev/python/docs/service-workers).

Before advertising arbitrary public URL support, add a local authenticated egress broker/proxy that resolves and checks the actual connection address, blocks loopback/private/link-local/metadata destinations including IPv6 and rebinding, and enforces destination scope at connection time. Disable unsupported direct transports and test that Chromium cannot bypass this path. This is a focused network boundary, not TLS interception or a full enterprise proxy. If that gate is not ready, ship the first PR on explicitly configured test/public destinations and say so. Local development origins belong to a separate user-confirmed capability, never the public browsing exception.

An arbitrary GET can mutate a server. Blocking POST alone neither proves read-only behavior nor supports all legitimate read APIs. “Public research mode” means no intentional account mutation or sensitive disclosure, with no authenticated account present; it does not mean zero HTTP-side effects. Authenticated pages have a stricter scope and may mark notifications read or record account activity. Strictly non-mutating account inspection requires a reviewed interface that provides that guarantee.

**Capture policy:** current one-shot, user-initiated capture remains the baseline. Proposed desktop sessions offer an explicit “Allow snapshots of this window for this task” control, expiry and visible stop indicator. Automatic scoped snapshots are a deliberate future policy change requiring updated AGENTS/security documentation; until accepted and implemented, each fallback capture stays user-initiated. Never silently convert existing consent into continuous screen capture. Cloud image sharing is included visibly in the consent or requires a separate confirmation.

## G. Approval / risk matrix

The renderer confirmation rule is preserved: an explicit scope confirmation may authorize several precisely bounded operations. It does not authorize any operation the model happens to consider useful. This requires documenting scoped confirmation as a policy evolution; until then, use per-operation confirmation for external/state-changing work.

| Operation/effect | Tier | Required authorization | Recovery |
|---|---|---|---|
| Read already observed public text, inspect approved UIA subtree | R0 | Existing observation scope; no new effect approval | Repeat within scope |
| Navigate/search public web; task-owned tab/scroll | R1 | Trusted public-research task grant, with domain/query/data/step limits | Observe current tab before recreating navigation |
| Launch/focus registered app or open approved document | R1–R2 | App/document scope, with exact confirmation initially | Check process/document identity first |
| Read private account or send private page content to a provider | R2 | Named account/site, data scope and model disclosure confirmation | Reobserve under same account; no automatic domain expansion |
| Fill fields on a real website | R2 | Approve exact values or selected profile/resume fields and receiving origin before first keystroke | Observe value; autosave/lost response may be unknown |
| Fill isolated test form with synthetic data | R1 | Bounded form-fill grant; explicit stop-before-submit limit | Inspect fields before changing anything again |
| Download to an approved folder | R1–R2 | Source/destination/size/type authorization; overwrite separately approved | Check temp/final manifest and hash |
| Copy/create/rename/move | R1–R2 | Exact operation or enumerated batch scope; both roots approved | File-ID/hash-based reconciliation |
| Send/upload/submit/publish/book | R2–R3 | Exact single-use digest-bound approval; supported verifier/reconciler | Never automatic repeat after uncertainty |
| Delete/trash, overwrite, account/security settings, payment | R3 | Exact approval, separately implemented capability; most deferred | No blind retry; deletion/setting-specific proof |
| Run project recipe | R3 | Exact recipe/project/revision approval, explicit arbitrary-repository-code warning | Existing process/run record and health check |
| Unknown control effect | At least R2 | Pause; exact approval only if effect can be described and supported; otherwise manual handoff | Unknown is not a safe-to-retry class |
| Password/OTP entry, UAC, shell escape, trusted Lumi controls | Denied to agent | Human takeover; unavailable even under broad task grant | No agent retry |

Typing is **not generally local and reversible**. Websites receive input events and may autosave immediately. A draft is also a disclosure. “Stop before submitting” guarantees Lumi does not intentionally invoke submission; it does not promise that entered data remains on the computer.

Approval cards show destination/account, action, exact data or local diff, scope limits and relevant uncertainty using controller-authored labels. Change recipient, content, account, price, target or policy scope and the approval is invalid. The agent cannot press its own approval controls using UIA, coordinates, keyboard shortcuts or browser automation.

## H. Browser session strategy

1. **First, public sessions:** one temporary, task-owned context retained across operations. No imported login state. Tabs have durable logical IDs; native handles are generation-scoped. Context disposal occurs at task end or explicit close.
2. **Then managed authenticated profile:** an opt-in, visible, dedicated profile under Lumi user data, locked to one worker. Users sign in manually. Stop automation and capture/provider sharing during login, OTP, CAPTCHA and password-manager UI. Resume only after the user clicks Continue and the account identity is reobserved.
3. **Profile persistence is not authority persistence:** cookies may survive restart, grants do not silently reactivate. Reload the task paused, fence old handles, obtain explicit Resume, and reobserve. Account switch/logout invalidates approval-dependent references.
4. **Public and private sessions remain separate:** a research grant cannot use the authenticated profile. Start with one authenticated profile and serial tasks; separate account profiles can follow when required. Do not claim that task IDs isolate cookies shared in the same browser context.
5. **Secret handling:** browser manages cookies; no `getCookies`, `storageState`, password or header tool. No cookie copies in PostgreSQL, prompts, HAR, traces or logs. Use restrictive profile-directory permissions. Windows Credential Manager/DPAPI may protect Lumi-owned secrets or an evidence encryption key; neither turns a complete browser profile into a secret-free artifact. Treat the entire profile as sensitive. Playwright explicitly warns that stored authentication state can impersonate a user. [Playwright authentication](https://playwright.dev/python/docs/auth).

Do not attach to the normal Chrome/Edge profile by default. Playwright documents that CDP attachment has lower fidelity than its native protocol and recommends a separate automation profile. Chrome changed default-profile remote debugging behavior starting with version 136. Do not promise to attach to an arbitrary normally launched browser or copy its cookie database. [Playwright browser types](https://playwright.dev/python/docs/api/class-browsertype), [Chrome remote debugging policy](https://developer.chrome.com/blog/remote-debugging-port).

A browser extension is a later option if “use the exact tab where I am already signed in” becomes essential. User-selected tabs, short-lived grants, narrow host permissions and authenticated native messaging would replace CDP exposure, but extension rollout, permission UX, worker suspension and recovery add a second transport. It is not the smallest next milestone.

Thus “already logged in” initially means **already logged in to Lumi’s managed browser**. Logins in the user’s normal browser require manual reauthentication in Lumi until a separate integration exists. Authentication failures and anti-bot blocks are honest limitations; no stealth bypass or CAPTCHA solver.

## I. Desktop automation, files and project execution

### Observation priority

For browsers: **scoped DOM + ARIA semantics → browser accessibility projection → selected-region image → coordinate fallback**. For native apps: **UIA control patterns and properties → selected-region image/OCR → coordinate fallback**. DOM and UIA are both structured semantic sources; “structured UIA before accessibility” is not a meaningful separate hierarchy.

Observe active window, an allowed-window inventory, process identity, title, bounds, focus, relevant UIA subtree, enabled/editable/password state and scroll position. Cursor position is useful only for input verification; it is not evidence of task success. Filter window titles before sharing; unrelated windows can reveal private information.

Send models only relevant visible text, semantic roles/names, opaque IDs, selected state, permitted source URLs and cropped images when needed. Keep full trees, raw screenshots, unselected windows, credentials, browser storage, filesystem absolute paths and process details local. OCR is a local fallback for text that UIA/DOM cannot expose; reuse local OCR infrastructure only after checking its screenshot suitability. Vision returns candidate targets/evidence, never authorization.

### Windows implementation

Use the existing Python distribution with a separate `app/desktop` helper and `pywinauto`’s UIA backend behind a small interface. Validate dependency installation against Python 3.12 and the packaged Windows build before committing to it. Prefer UIA Invoke, Value, Selection and Scroll patterns; use Win32 only for window enumeration/focus and bounded input fallback. [pywinauto UIA guide](https://pywinauto.readthedocs.io/en/latest/getting_started.html).

Run UIA on a dedicated COM MTA thread and enforce process-level watchdog timeouts; a hung accessibility provider must not freeze main or the runtime. Microsoft describes UIA threading hazards and the separate-thread requirement. [Microsoft UIA threading guidance](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-threading).

Do not request administrator privileges or `uiAccess` to control elevated windows. UAC, secure desktop, password surfaces, browser DevTools and Lumi’s approval UI are excluded. Microsoft places special restrictions on UIAccess; it is not a general-purpose bypass for an automation agent. [Microsoft UIA security](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-securityoverview).

Coordinate fallback binds a proposal to one capture, exact foreground window/process, client-space point, DPI and geometry. Require a recent scoped capture, visibility/hit-test checks where available, no overlays and no intervening human input. Invalidate after movement, scrolling, navigation or focus change. Never coordinate-click a high-impact/unknown control automatically. Low-confidence target resolution pauses. Add drag and unrestricted key combinations last, not as an escape hatch for missing semantic APIs.

### Filesystem boundary

Reuse approved-root and dropped-file trust separately. A dropped resume authorizes that file, not its parent directory. Add per-root read/create/modify permissions; existing search approval does not imply write permission.

Resolve by file ID and root ID; revalidate final path and file identity immediately before use. For writes, check destination parent by handle, reject junction/reparse traversal, `..`, device paths, alternate data streams, UNC paths unless separately supported, and reserved names. Recheck after opening, use atomic no-overwrite creation, and avoid name-only check/use races. Existing `realpath` checks are a foundation, not complete write-race protection.

Downloads go to a task-owned quarantine directory with size/time/type limits and a durable transfer manifest. Verify completion, MIME/signature where appropriate, hash and intended filename before an atomic move into the approved root. Do not auto-open executables, scripts, shortcuts or macro documents. Preserve Windows download provenance where supported. Disallow overwrite by default. Cross-volume moves are copy/verify/delete workflows, not atomic rename; defer them initially. First deletion capability should be reviewed recycle-bin/trash with exact confirmation, not recursive permanent deletion.

Resume comparison requires a sandboxed/bounded PDF/DOCX/text extraction subprocess, content-size/page limits, no macros or external-link fetching, source references and explicit provider disclosure scope. Local readable text is still untrusted task data.

### Terminal and coding actions

Choose **B: a restricted structured command tool**, with a trusted project recipe. Visible-terminal typing is not safer: entering a command through UIA grants shell authority just as surely as a shell API does.

Stage it:

* Launch registered VS Code with an approved workspace; do not accept workspace-trust dialogs or run tasks automatically.
* Add a recipe approved in Lumi’s trusted UI: project root, fixed executable/interpreter, argument vector, working directory, environment allowlist, script/lockfile hashes, expected process/health endpoint, timeout and stop policy. Pin a user-created recipe revision, not a model-created command.
* Main spawns and supervises that exact recipe, removes inherited provider/runtime/database secrets, records the run before spawn, and uses Windows job/process lifecycle support. Expose bounded log tails as untrusted data. The user can inspect progress in Lumi or VS Code.

On Windows, `npm.cmd` is not a native executable that can simply be passed to `execFile`. Prefer a validated `node.exe` plus the installed `npm-cli.js` entry point, or a tightly fixed launcher whose quoting is tested. Node documents the `.cmd`/shell distinction. [Node child-process documentation](https://nodejs.org/api/child_process.html).

Crucially, `npm run dev` executes repository scripts, hooks and dependencies, potentially with user-level authority. The confirmation must say that. A structured launcher limits what the model can request; it does not sandbox arbitrary project code. Do not offer unrestricted commands, terminal text-entry fallbacks, package installation, Git push or automatic project configuration fixes in this milestone.

## J. Verification, recovery and reconciliation

| Effect | Verify before reporting success | Restart / lost-response behavior |
|---|---|---|
| Navigation | Expected canonical destination and frame/account; page ready predicate; actual content, not only load event | Inspect owned tab first. Recreate public navigation only within live scope; private navigation may have incidental effects. |
| Page extraction | Correct page identity, source URL/time and supporting text spans; distinguish rating from rank/solved count | Repeat read under scope; never substitute memory for missing evidence. |
| Form fill | Actual current field values/selection and validation state; mandatory fields accounted for; submit not invoked | Observe first. Set-to-value is safer than append; autosave may require reconciliation. |
| Download | Browser completion plus transfer ID, byte length, file signature and hash; final approved path exists | Inspect manifest/temp/final files; never invent a second filename to hide uncertainty. |
| File create/copy/move/rename | Source/destination file identity and expected content hash; source absence only where operation requires it | Handle-based observation and operation manifest; partial cross-volume operation is not success. |
| App launch | Correct executable/process creation identity plus intended window/document | Locate existing app/run before spawning another; PID alone is insufficient. |
| Submission/job application | Account, recipient/job ID, exact payload, submission reference and authoritative application record | Receipt/status lookup; no second submission because the form is visible or history is empty. |
| Message | Service message ID plus account/channel/recipient/content correlation | Read-only sent-item lookup. Ambiguous timestamp/text match stays unknown. |
| Booking | Existing reference and reviewed receipt/lookup checks | Preserve current `lookup_booking` and per-site authoritative-absence rule. |
| Project command | Owned run ID, process tree, bounded logs and expected readiness/health; exit code for finite commands | Check run/process/port ownership before restart. A missing process does not prove no files or remote systems changed. |

Playwright actionability checks help ensure input reaches a usable element, but do not prove business completion. Use its normal checks and assertions; no forced input to “get past” a failure. [Playwright actionability](https://playwright.dev/python/docs/actionability).

Keep the existing recovery rule for any unfinished execution attempt: fence old workers, mark uncertainty, and reconcile. Add a side-effect-free observation step for local navigation/focus actions; if the exact old effect cannot be proven, it may stay unknown while policy permits a new safe local action. Never rewrite uncertainty as a historical failure just to make the loop proceed.

Generalize reconciliation into a small closed registry: each strategy declares correlation keys, data authority, allowed read operations and whether absence is authoritative. Local file identity can provide strong evidence; a website toast or screenshot usually cannot. A model may extract candidate evidence but cannot issue the verdict by confidence score.

Retry classes:

* **Repeat reads:** passive DOM/UIA inspection, approved file reads, pure model calls. No effect attempt is replayed.
* **Observe then repropose:** tab activation, focus, scrolling, local field state under a proven bounded policy. The new action gets its own authorization.
* **Reconcile before any new effect:** downloads, file moves, draft autosave, app/project start.
* **Never automatically repeat:** submission, sending, booking, upload, deletion, payment, commands with uncertain effects, unknown activation.

An authoritative no-effect result closes the original action as failed. Any consequential retry is a **new** action with a link to the old one and a fresh exact approval; never reuse a consumed approval. A failed lookup or eventually consistent “not found” leaves the original unknown. Bound reconciliation attempts and offer manual review; do not mark an unresolved task succeeded or quietly unblock duplicate actions.

Cancellation/Stop revokes future dispatch and input, pauses the browser/worker where possible, and preserves evidence. It cannot undo an HTTP request already sent. In-flight work is settled or marked unknown, even if the user closes the panel. Do not automatically compensate by deleting a booking or sending a correction; compensation is another effect requiring approval.

## K. Performance, tokens and multimodal routing

Use event-driven invalidation and bounded snapshots, not continuous full DOM and screenshot uploads. Browser navigation/frame/load and selected DOM changes invalidate relevant observations. UIA focus/property/structure events flag a refresh. Events are hints; always revalidate before input.

Maintain a local canonical observation with stable subtree hashes. Send an initial filtered snapshot, then a bounded delta; include removals and current epoch. On restart, missed sequence or excessive churn, send a fresh snapshot instead of applying an unreliable diff. Filter advertisements, offscreen repetition and irrelevant subtrees without silently omitting warnings or form errors. Every truncated observation declares truncation and supports bounded expansion.

Suggested initial per-call limits: 200 semantic nodes, 20 links, 12 KB selected text, 6,000 input tokens, 600 output tokens. One relevant crop rather than full-screen images; retain enough surrounding context to identify the window and control. Size pixels and image budget separately—JPEG byte size is not a token budget.

Cache only safe extraction results by session/account, document epoch and content hash. Never cache target freshness, authorization or successful verification across navigation. Avoid caching signed URLs and credentials. Keep raw screenshots in memory by default; if explicitly retained for recovery/support, use an encrypted local store with a short TTL and protected key. PostgreSQL holds redacted facts/hashes/references; normal diagnostics hold IDs, counts and codes. Missing expired evidence means reobserve, not replay.

| Work | Preferred implementation |
|---|---|
| Voice | Existing OpenAI Realtime / Gemini Live transports for speech and turn intake. No independent computer-control loop in the voice model. |
| Dates, budgets, scope, URL/path checks, field equality | Deterministic code. Extend relative-date handling for a user-local “this week” interval. |
| Simple extraction and short summaries | Deterministic selectors/structured text first; configured low-cost text route when needed. |
| Next browser/desktop action | One strict-JSON planner route using the existing model router, selected by fixture/live task success rather than vendor preference. |
| Ambiguous DOM or difficult planning | One stronger reasoning escalation after gathering missing evidence. It receives the same capability limits. |
| Visual understanding | Existing image-capable Gemini/OpenAI routes with approved crops. Text-only routes, including DeepSeek where configured that way, receive no images. |
| Verification/policy | Deterministic code and reviewed evidence rules; models can suggest/extract, never authorize or attest authority. |

Keep current providers and configurable model names; do not make a new model migration part of this milestone. Verify availability and pricing when implementing, not by treating repository defaults as current recommendations. Provider failover is also a data-routing decision: private content may go only to providers explicitly permitted by the task’s disclosure policy.

The present context builder uses characters/4 and always includes some required fields; this is not a strict multilingual token guarantee. Add provider-aware counting where available, conservative byte/character caps elsewhere, and fail closed if required policy/objective content cannot fit. Never truncate security rules or quietly discard the user’s stop-before-submit constraint.

Track p50/p95 observation/model/action latency, bytes/tokens per completed task, verification failures and pauses. Target under 500 ms for bounded local observations on fixture hardware and no unnecessary model call after deterministic verification; treat targets as measurements to establish, not performance promises.

## L. Milestone plan

Implement each stage in independently useful PRs. The next agreed product milestone should be **M7: public browser research**, not all desktop capabilities at once. Later stages are a roadmap requiring their own scope agreement.

### M7a — One approved URL, one grounded answer

**Capabilities:** user provides a public HTTPS URL and question; trusted UI confirms inspection; generic browser extraction returns source-grounded text and a bounded answer. Controlled origins initially; no free-form browsing loop.

**Likely files:** new `services/agent/app/browser/operations/public_page.py`; new browser target/observation contracts; existing `registry.py`, `protocol.py`, `worker.py`, `services/browser_execution.py`, `api/{schemas,routes,contract}.py`; `src/shared/agent-contracts.ts`, `agent-runtime-contract.json`, `src/main/services/{agent-wire,agent-tasks,agent-ipc,agent-runtime-supervisor}.ts`, `src/preload/index.ts`, `AgentTaskPanel.tsx`; new main computer-request/parser module; existing model router and context builder.

**Tests:** arbitrary-operation rejection, URL scheme/redirect rules, bounded extraction, hostile content, exact approval/digest, duplicate request, lost observation, restart and unchanged booking acceptance. No provider required for deterministic fixture tests.

**Exit:** approved test page inspection works end-to-end through renderer/preload/main/runtime/worker and survives restart honestly. New operation has no selectors/scripts supplied by model. Existing booking remains behaviorally unchanged.

**Excluded:** login, clicking, search, multi-step planner, desktop, file writes, cookie access and arbitrary internet coverage until egress gates pass.

### M7b — Public research across sites

**Capabilities:** scoped research grant; general public navigation, search, text/links, scroll, back/forward, task-owned tabs; one-action planner; LeetCode/GitHub/job research. Ephemeral task session persists between steps. Network egress boundary and strict task budgets.

**Likely files:** new `app/services/computer_tasks.py`, `app/domain/computer.py`, `app/services/computer_policy.py`, `app/browser/{observations,targets,network_policy,sessions}.py`; new egress helper; migration `0004` or next available for grants/step authorizations/observations/session metadata and dispatch changes; `services/actions.py`, repositories, recovery, contract generation; new `src/main/agent/computer-planner.ts`, shared computer contracts and renderer scope/timeline views. Keep file naming aligned with the eventual implementation review.

**Tests:** public network escape suite, multi-tab ownership, navigation epochs, dynamic DOM, request deduplication, grant expiry/revocation, model failure, prompt injection, budgets, stuck loops, browser/runtime/main crash and migration compatibility.

**Exit:** A/B/D acceptance below; a held-out public research task on an unadapted site completes with cited evidence; no fixture-specific selectors in generic operation code. No unauthorized filesystem/account effects in adversarial tests. Packaged profile and cleanup tested.

**Excluded:** login, generic arbitrary buttons, form fill, download/upload, desktop control, automatic applications. This is a general public web research agent, not yet a broadly interactive browser agent.

### M8 — Authenticated browsing and bounded preparation

**Capabilities:** dedicated opt-in persistent profile; manual sign-in; account-scoped reading; semantic field/selection actions with disclosure approval; safe test job-form completion stopping before submit. Approved PDF download to an approved root can be a separate PR in this stage. Keep real consequential execution restricted to reviewed supported descriptors.

**Likely files:** browser sessions/targets/policy and new interaction operations; approval UI and data manifest; supervisor/packaging config; new main transfer/file broker; existing approved-root store/search code; new bounded document extraction helper if resume comparison is included; schema metadata for evidence and transfers.

**Tests:** session persistence/logout/account switch, login capture suspension, autosave traps, onfocus/onchange effects, hidden submission, Enter key, private query exfiltration, popup/download handling, unsafe filenames, form stop condition, profile locking and packaging.

**Exit:** E acceptance plus manual login/restart/account-inspection; no secrets in provider payloads/logs; entered values verified; synthetic fixture submission count remains zero. General interaction demonstrated across several unadapted form layouts.

**Excluded:** copying normal browser cookies, extension/CDP attachment, arbitrary submit/send/purchase, CAPTCHA bypass, payments, destructive account changes and blanket all-site login authority.

### M9 — Windows semantic computer use

**Capabilities:** window inventory/focus, registered app launch, UIA observation/invoke/value/selection/scroll, scoped snapshots and limited region-based fallback. Demonstrate an editor and a file-oriented application using generic semantics. VS Code launch can ship before project execution.

**Likely files:** new `services/agent/app/desktop/{worker,protocol,uia_backend,targets}.py`; managed-worker supervision, worker-generation/dispatch schema generalization; new main app broker; existing capture service and renderer capture consent; `scripts/build-agent-runtime.mjs` and Python dependency lock.

**Tests:** dedicated Windows fixture app, changing IDs/labels, modal overlays, stale HWND/PID reuse, missing UIA patterns, COM hangs, elevated-window refusal, DPI/multi-monitor transforms, human input takeover, forbidden Lumi targeting, input loss/crash.

**Exit:** native semantic actions on at least two app frameworks plus reproducible fixture coverage; no blind coordinate fallback; pause/stop works; worker hangs do not hang main; installed build passes on Windows 10/11 x64 where supported.

**Excluded:** admin/UAC, remote desktop, arbitrary hotkeys/dragging, background surveillance, terminal typing and unrestricted app control.

### M10 — Bounded cross-app tasks and project recipes

**Capabilities:** approved document read/compare, controlled download/file placement, browser-to-document-to-form preparation, registered project start/status/stop, selected verified consequential test action. Reuse the existing booking adapter rather than inventing a new real-world submission target.

**Likely files:** new main `file-broker`, `project-runner`, `run-recipes`; document parser helper; generalized execution/reconciliation descriptors; existing file/root store, windows job supervision, task UI, shared contracts and eval fixtures.

**Tests:** root escape/write race, protected source disclosure, partial transfer, concurrent destination changes, recipe hash changes, environment stripping, duplicate launch, crash after external effect and cross-executor effect lock.

**Exit:** C/F acceptance and a combined download → inspect → compare → prepare workflow with evidence at each boundary. Any ambiguous effect pauses and cannot be bypassed through a new task/tool.

**Excluded:** general shell, dependency installation/repair, arbitrary Git modification/push, permanent recursive delete, extension marketplace, workflow marketplace, multiple concurrent desktop agents.

**Planning scale:** roughly 3–5 engineer-days for M7a, 8–12 additional days for M7b, then 8–12 for M8, 8–12 for M9 and 5–10 for the selected M10 workflows, plus explicit packaging/field-test contingency. These are sequencing estimates, not commitments; network containment, login behavior and UIA coverage are the main uncertainty. The useful public-research jump should ship before investing in the whole roadmap.

## M. First implementation slice

**First PR: “Inspect a user-provided public page through the durable action pipeline.”**

Use the exact-approval mechanism already present. The user enters a URL and question; Lumi shows “Open and inspect this page,” including hostname and model disclosure. Confirming creates/claims the existing approval and executes one generic `inspect_public_page` operation. The worker internally composes navigation and bounded observation; this remains a useful one-shot operation after generic navigation is introduced.

The PR proves five seams:

1. A strict, URL-bearing task type can cross existing boundaries without permitting scripts, selectors or arbitrary routes.
2. Browser execution is selected through a reviewed descriptor instead of being hard-coded exclusively to booking; booking behavior is preserved.
3. One observation envelope supplies source identity, bounded text, links, version and evidence references.
4. The UI can distinguish “page inspected” from “question answered”; answer generation happens only after evidence and its authorized provider disclosure exist.
5. Lost results and restart preserve the action/attempt history; a repeated read is a separately authorized observation, not reuse of an old approval.

Use a deterministic dynamic public-profile fixture with rating, rank and solved count as distinct labels, plus malicious-text and redirected-page variants. An explicitly configured public origin can be a manual smoke test; do not claim broad internet support before M7b’s egress controls. Missing/hidden rating produces “not visible in the observed page,” never a guessed rating.

Do not add a grant migration, full planner, new browser framework, LeetCode-specific extraction adapter, UIA or a new provider in this PR. Existing immutable JSON proposals can carry the versioned observation specification; add only the contract/schema changes actually required. General session support arrives in M7b. This keeps the first PR reviewable without building throwaway domain code.

## N. Acceptance and test strategy

### Deterministic layers

| Layer | Required coverage |
|---|---|
| Contract/policy units | Every operation union; unknown fields; forged references; risk derivation; stale digests; disclosure source/recipient; grant limits; URL/path canonicalization; multilingual token caps |
| PostgreSQL integration | Single-use exact approval and step authorization; competing execute/revoke/cancel; revision conflicts; one dispatch per attempt; unresolved-effect locks; schema migration from existing tasks |
| Browser fixtures | Multi-tab/history, extraction, dynamic content, SPA navigation, iframe/popup/download, auth persistence, account switching, autosave, misleading buttons, GET mutation, delayed receipts, eventual consistency |
| Windows fixtures | UIA trees/actions, multiple framework controls, missing accessibility, overlays, focus theft, DPI/multi-monitor, process restart, hung COM, credential/elevated/trusted-UI exclusion |
| Visual fallback | Fixed synthetic screenshots with known targets, uncertainty thresholds, crop transforms and stale-frame rejection; deterministic mocked vision in CI, separate live vision eval |
| Fault injection | Kill before claim, after claim/before dispatch, after effect/before reply, after reply/before DB commit; independent browser/worker/main/runtime crashes; DB loss; duplicate and late RPCs |
| Planner/provider | Malformed output, refusal, timeout, fallback, wrong target, invented IDs, unsafe URL, repeated loops, budget exhaustion; deterministic scripted plans separate from real-provider capability tests |
| Security fixtures | Instructions in DOM/ARIA/PDF/OCR/images; injected approvals; data exfiltration via query/form; local-network redirect/rebinding/IPv6; cookie/log leakage; attempt to use desktop input to approve |
| Packaged acceptance | Real renderer → preload → main → runtime → executor; browser/profile/worker lifecycle, upgrades, resource paths, native imports, signed distribution path and no bundled secrets |

Do not count deterministic scripted-model success as proof that a live model plans well. Maintain a small held-out set across unrelated sites/layouts and run opt-in real-provider tests with synthetic accounts. Avoid brittle selectors in policy tests; mutate DOM labels and layouts independently of effect behavior.

Run `npm.cmd run typecheck`, `npm.cmd test`, `npm.cmd run build`, Python `uv run pytest` / `uv run mypy`, and `npm.cmd run eval` after significant implementation changes. Run `npm.cmd run package` and installed acceptance when introducing profiles, executors or native dependencies. Report pre-existing failures separately; no blanket green claim. Avoid real account mutations in routine CI.

### Manual A–F acceptance

**A. “Open my public LeetCode profile and tell me my rating.”**

User supplies profile URL/handle through trusted input; Lumi never guesses identity from another account. Confirm public research. Open actual profile, distinguish contest rating from rank and solved count, and answer with URL plus observation time. If no rating is published or the site blocks access, state that. Pass requires a grounded answer or correct limitation, with no login, mutation or domain adapter. Exercise a rated and unrated profile.

**B. “Search GitHub for my Lumi repository and summarize it.”**

Use user-provided owner identity or clarify among candidates. Search publicly, open the selected repository and inspect README/file listing. Summarize observed purpose and structure with links. An identically named repository must not be mistaken for the user’s. Private repository access requires a separate authenticated scope.

**C. “Open VS Code and start Lumi.”**

User approves project root, VS Code app and exact run recipe. Verify workspace, start one supervised process tree and confirm the expected app/health signal. Request again and restart Lumi during the run: no duplicate development server. A changed package script invalidates recipe approval. Missing dependencies or environment produce a precise blocker, not automatic installation or arbitrary terminal commands.

**D. “Search for junior AI-agent jobs posted this week and open relevant tabs.”**

Resolve an explicit local calendar interval using the trusted clock; on 17 September 2026 with Monday-based week semantics, “this week so far” is 14–17 September in Asia/Kolkata. Display the interpretation and honor user correction. Extract role, employer, location, source and posting date; unknown dates remain unverified, not silently included as confirmed. Open at most five relevant deduplicated tabs. No applications, signup or resume disclosure.

**E. “Fill a safe test job-application form but STOP before submit.”**

Use a synthetic resume and controlled form. Confirm selected fields and destination; verify visible values and required errors. Fixture counts submissions, autosaves and uploads independently. Expected final submissions: **zero**, including implicit Enter and onchange traps. For a trap with unavoidable transmission, pause before entry and explain the limitation. A real resume requires document-read/provider/recipient authorization before leaving the machine.

**F. Crash immediately after a consequential test action.**

Use the existing booking fixture or a controlled job fixture with an authoritative correlation ID. Wait until fixture storage confirms the effect, suppress the response, then kill Lumi/worker. Restart: one original attempt, one external submission, one effect; action unknown until read-only reconciliation. Duplicate dispatch, new task, alternate primitive and model-provider fallback must not repeat it. Also test non-authoritative “not found”: action must remain unknown and blocked.

Additional real manual checks: managed-login persistence without exposing passwords; authenticated account switch; PDF download to approved Projects root; UIA interaction in an editor plus another native app; deliberate user focus takeover; screenshot consent revocation; network destination blocking. Each records expected behavior, actual evidence and unresolved limitations.

## O. Risks and likely failure modes

* **False read-only claims:** arbitrary navigation and typing can have server effects. Separate public research from authenticated work; never derive safety solely from HTTP method or button label.
* **Overpromising duplicate protection:** one local attempt is not exactly-once remote execution. Remote correlation/idempotency and authoritative evidence determine what can be concluded.
* **Confirmation fatigue:** narrow grants remove routine browsing prompts; unknown/high-impact effects remain explicit. Measure prompts per completed task, not just raw task success.
* **Scope that is too broad:** a grant combining private files, arbitrary destinations and generic input is effectively an exfiltration capability. Separate data sources and recipients and keep public research isolated.
* **UI race and stale identity:** DOM replacements, recycled handles, overlays, human input and account switches invalidate action targets. Reobserve and pause instead of clicking the nearest plausible element.
* **Authentication/anti-bot friction:** some sites will not work in automation profiles. Manual takeover and honest unsupported status are product requirements.
* **No universal reconciliation:** arbitrary sites may never provide reliable absence. Leave commitments unsupported or uncertain; do not generate a plausible reconciliation script.
* **Secret exposure in rich evidence:** screenshots, signed URLs, DOM attributes and logs can leak more than current typed clinic facts. Filter before persistence/provider routing; disable raw tracing by default.
* **Running code masquerading as a harmless app action:** VS Code tasks/extensions and npm scripts can execute arbitrary code. Keep workspace trust and recipe execution explicit.
* **Packaging constraints:** new UIA/native components may encounter Application Control, architecture and COM issues. Test a real installed build early in M9.
* **Architecture bloat:** avoid a plugin framework, vector-memory rewrite, multi-agent orchestrator, generic workflow DSL or parallel state store. One planner, one ledger and three narrow executor boundaries suffice.
* **Safety tests proving only fixtures:** augment deterministic adversarial tests with held-out live tasks. Do not broaden supported commitments merely because synthetic forms passed.

## P. Decision log and conclusion

| ID | Decision | Revisit when |
|---|---|---|
| D1 | Keep Electron/Python/PostgreSQL/Playwright and existing booking flow. | A measured bottleneck prevents a concrete acceptance test. |
| D2 | Generic closed primitives plus retained domain adapters. | A new primitive is genuinely necessary, not a synonym. |
| D3 | Public ephemeral and private managed profiles are separate. | Exact existing-tab integration becomes a validated product need. |
| D4 | One planner; deterministic authority and verification. | Measured task performance justifies another reasoning role. |
| D5 | Exact approvals first; explicit scoped-grant migration next. | M7b implementation review, including AGENTS/security-policy update. |
| D6 | No arbitrary selectors, JS, shell, raw paths or unrestricted input. | No planned relaxation; add reviewed capabilities instead. |
| D7 | General preparation before general submission. | A specific consequential effect has adequate verification/reconciliation. |
| D8 | Python UIA helper, same integrity level, semantic first. | Packaged spike reveals a concrete compatibility/performance problem. |
| D9 | Recipe-based project execution with explicit code-execution consent. | A separate, threat-modeled sandboxed coding milestone is approved. |
| D10 | Unknown outcomes remain unknown; no repeated effect through a new task. | Only authoritative evidence resolves the uncertainty. |
| D11 | User-initiated capture remains; scoped snapshot consent is an explicit future change. | Desktop milestone scope and policy are accepted. |
| D12 | General-network coverage is gated by actual egress tests. | No weakening based only on successful page navigation. |

1. **Recommended architecture:** an incremental capability-controlled executor on Lumi’s existing durable ledger, using Playwright for browser semantics, UIA for Windows semantics, and main-owned file/app/recipe brokers. Models propose bounded actions; trusted policy and confirmed scope authorize them.
2. **First thing to build:** one approved public-page inspection with a grounded answer through the existing action/approval/attempt pipeline. Then expand to bounded public research as the next milestone.
3. **What not to build:** an unrestricted computer tool, an LLM shell, automatic normal-profile attachment, universal website submission, a replacement orchestration platform, or a new domain adapter for every website.
4. **When “general browser agent” is truthful:** M7b is a general public web research agent. The broader label becomes justified at M8 when Lumi can navigate and prepare interactions on previously unadapted sites, maintain scoped sessions, verify progress and stop safely at unsupported effects. State clearly that general browsing does not mean universal authenticated submission.
5. **When “general computer-use agent” is truthful:** after M9’s semantic native-app coverage and M10’s bounded cross-app workflow pass in a packaged build, with actual browser + native app + filesystem work, interruption and recovery. Merely launching VS Code or adding coordinate clicks does not meet that bar.
