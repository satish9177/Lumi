#!/usr/bin/env node
/**
 * Lumi's deterministic evaluation suite (Milestone 6).
 *
 * Every case is an existing automated test, grouped by the behaviour it
 * proves. Nothing here calls a paid model: providers are scripted, the
 * realtime voice is scripted, and the browser drives the local fixture site.
 * Live provider checks are separate (docs/EVALS.md).
 *
 *   npm run eval            # TypeScript + Python (needs TEST_DATABASE_URL and Chromium)
 *   npm run eval -- --ts    # TypeScript cases only
 *
 * Writes dist/evals/eval-report.json and prints a scorecard. Exits non-zero
 * if any case failed or could not be found.
 */

import { spawnSync } from 'node:child_process'
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const AGENT = join(ROOT, 'services', 'agent')
const OUT = join(ROOT, 'dist', 'evals')
const tsOnly = process.argv.includes('--ts')

const ts = (file, test) => ({ runner: 'vitest', file, test })
const py = (file, test) => ({ runner: 'pytest', file, test })

const CASES = {
  'voice/task': [
    ['ordinary conversation does not create a task', ts('src/main/models/model-routing.test.ts', 'ordinary conversation creates no task')],
    ['English request becomes a dated durable search', ts('src/main/agent/m6-controller.test.ts', 'resolves "Saturday" to the calendar date')],
    ['ambiguous date is asked back, nothing created', ts('src/main/agent/m6-controller.test.ts', 'asks instead of guessing')],
    ['refinement withdraws an excluded prepared booking', ts('src/main/agent/m6-controller.test.ts', 'a date refinement withdraws')],
    ['compound request prepares exactly one booking', ts('src/main/agent/m6-controller.test.ts', 'searches, chooses the cheapest')],
    ['compound request stops before approval', ts('src/main/agent/m6-controller.test.ts', '"find the first one and book it"')],
    ['attempted voice approval only surfaces the card', ts('src/main/agent/m6-controller.test.ts', '"book it" afterwards only surfaces the card')],
    ['duplicate (replayed) turn runs nothing again', ts('src/main/agent/m6-controller.test.ts', 'a replayed compound turn runs nothing again')],
    ['replayed request after restart creates no second task', ts('src/main/agent/m6-controller.test.ts', 'a replayed typed request after a main restart')],
    ['voice turn ids stay unique across reconnects', ts('src/main/voice/gemini-live.test.ts', 'globally unique turn ids')],
    ['late transcript still binds the call to its own turn', ts('src/main/voice/gemini-live.test.ts', 'binds a tool call to the new turn')]
  ],
  browser: [
    ['normal booking succeeds exactly once', py('tests/test_browser_booking.py', 'test_an_approved_booking_is_made_exactly_once_and_verified')],
    ['price changed after approval: nothing booked', py('tests/test_browser_booking.py', 'test_a_changed_price_blocks_the_booking_entirely')],
    ['slot disappeared: known failure, no side effect', py('tests/test_browser_booking.py', 'test_a_disappeared_slot_is_a_known_failure_with_no_side_effect')],
    ['hostile page text has no authority', py('tests/test_browser_booking.py', 'test_page_text_telling_lumi_to_ignore_its_rules_has_no_authority')],
    ['worker crash after submit is unknown, not failed', py('tests/test_browser_lost_response.py', 'test_a_worker_killed_after_submitting_leaves_an_unknown_not_a_failure')],
    ['response lost after the external effect', py('tests/test_browser_lost_response.py', 'test_a_booking_that_succeeded_before_everything_died_is_reconciled_not_repeated')],
    ['second workflow is read-only and leaves no ledger footprint', py('tests/test_m6_runtime.py', 'test_clinic_info_lookup_records_typed_facts_without_any_action')],
    ['date window is applied to observed slots', py('tests/test_m6_runtime.py', 'test_search_applies_the_date_window_to_observed_slots')]
  ],
  recovery: [
    ['hard kill mid-execution becomes OUTCOME_UNKNOWN', py('tests/test_outcome_unknown_recovery.py', 'test_a_hard_killed_execution_becomes_outcome_unknown_and_is_reconciled')],
    ['reconciliation is read-only and finds the booking', py('tests/test_browser_booking.py', 'test_reconciliation_finds_a_booking_without_making_another')],
    ['recovered action can never be executed again', py('tests/test_browser_lost_response.py', 'test_a_recovered_browser_action_cannot_be_executed_again')],
    ['no duplicate submission under concurrency', py('tests/test_browser_booking.py', 'test_concurrent_executions_of_one_action_produce_one_booking')]
  ],
  provider: [
    ['malformed or hostile model output is refused', ts('src/main/models/model-routing.test.ts', 'malformed or hostile model output is refused')],
    ['provider timeout fails over; cooldown applies', ts('src/main/models/model-routing.test.ts', 'fails over on timeout, unavailability and malformed output')],
    ['fallback provider keeps one task and one action', ts('src/main/models/model-routing.test.ts', 'provider A times out mid-task')],
    ['all providers down: deterministic fallback, no extra work', ts('src/main/models/model-routing.test.ts', 'when every provider fails')],
    ['refusal is not shopped to another model', ts('src/main/models/model-routing.test.ts', 'stops at a refusal')],
    ['realtime session drop is reported for reconnect', ts('src/main/voice/gemini-live.test.ts', 'reports a dropped session as a transport failure')],
    ['provider errors never carry provider bodies or keys', ts('src/main/models/providers-http.test.ts', 'classifies failures without ever carrying the provider body')]
  ],
  'memory/context': [
    ['stale preference is overridden by the current request', ts('src/main/agent/m6-controller.test.ts', 'a remembered preference fills a gap but never overrides')],
    ['website fact overrides remembered value', ts('src/main/agent/m6-controller.test.ts', 'the website, not memory, decides the price')],
    ['long history stays within the context budget', ts('src/main/models/model-routing.test.ts', 'keeps a very long history inside the class budget')],
    ['task state is summarised, not the whole ledger', ts('src/main/models/model-routing.test.ts', 'summarises durable task state')],
    ['tampered memory entries are ignored', ts('src/main/models/providers-http.test.ts', 'ignores tampered entries')]
  ],
  security: [
    ['renderer never sees provider credentials', ts('src/main/agent/security-boundary.test.ts', 'contains no key names, token shapes')],
    ['Gemini token stays in main; relay validates every message', ts('src/main/voice/gemini-live.test.ts', 'keeps the token in main')],
    ['model cannot request generic browser/HTTP/shell', ts('src/main/agent/security-boundary.test.ts', 'rejects')],
    ['tool vocabulary has no approve/execute/URL/selector', ts('src/main/agent/security-boundary.test.ts', 'the voice tool vocabulary has no approve')],
    ['voice cannot approve (no approval command exists)', ts('src/main/agent/security-boundary.test.ts', 'main re-validates renderer commands')],
    ['hostile page text cannot become a controller instruction', ts('src/main/agent/security-boundary.test.ts', 'hostile page text quoted back')],
    ['hostile profile text is never spoken', ts('src/main/agent/m6-controller.test.ts', 'page text that is not a plain value is never spoken')],
    ['preload exposes only fixed channels', ts('src/main/services/agent-boundary.test.ts', 'preload exposes only the fixed agent channels')]
  ],
  // Milestone 7a: one approved URL, one grounded answer.
  inspection: [
    ['refused destinations never reach the runtime', ts('src/main/services/agent-inspection.test.ts', 'refuses file:///C:/Windows/win.ini before contacting the runtime')],
    ['the page is read once, only after the trusted approval', ts('src/main/services/agent-inspection.test.ts', 'reads once only after the trusted approval')],
    ['"yes / approve / go ahead" only points at the card', ts('src/main/services/agent-inspection.test.ts', 'only points at the card')],
    ['duplicate request after main restart shows the same card', ts('src/main/services/agent-inspection.test.ts', 'a duplicate typed request, even after a main restart')],
    ['lost response: answered later from the saved page, no reopen', ts('src/main/services/agent-inspection.test.ts', 'a lost execution response is not retried')],
    ['rating is answered from its label, not rank or solved count', ts('src/main/agent/page-answer.test.ts', 'finds the rating, not the rank or the solved count')],
    ['missing rating is "not found", never guessed', ts('src/main/agent/page-answer.test.ts', 'a missing rating is not found, never guessed')],
    ['ungrounded or action-bearing model output is refused', ts('src/main/agent/page-answer.test.ts', 'refuses')],
    ['a model persuaded by a hostile page is refused', ts('src/main/agent/page-answer.test.ts', 'a model persuaded by a hostile page is refused')],
    ['page text reaches only approved providers', ts('src/main/agent/page-answer.test.ts', 'sends page text only to providers the approval named')],
    ['URL policy: schemes, credentials, local and IP destinations', py('tests/test_public_url_policy.py', 'test_refused_destinations_fail_closed_with_a_stable_code')],
    ['page scripts cannot send POST/PUT/PATCH/DELETE; GET content still loads', py('tests/test_network_guard_methods.py', 'test_the_guard_refuses_and_records_every_mutation_method')],
    ['hostile page: every exfiltration channel is closed', py('tests/test_public_page_worker.py', 'test_hostile_text_is_only_data_and_every_exfiltration_channel_is_closed')],
    ['forbidden redirects are refused before they are requested', py('tests/test_public_page_worker.py', 'test_a_forbidden_redirect_is_refused_before_it_is_requested')],
    ['dynamic page: document epoch and late content', py('tests/test_public_page_worker.py', 'test_a_page_that_replaces_itself_is_observed_at_its_new_document_epoch')],
    ['the operation accepts nothing but a URL', py('tests/test_public_page_worker.py', 'test_the_operation_accepts_nothing_but_a_url')],
    ['approved read stores one bound observation; consumed approval refused', py('tests/test_page_inspection_ledger.py', 'test_an_approved_inspection_reads_once_and_stores_one_bound_observation')],
    ['stale revision and concurrent execution read once', py('tests/test_page_inspection_ledger.py', 'test_a_stale_revision_and_a_concurrent_second_execution_read_nothing_extra')],
    ['kill before dispatch: unknown after restart, new approval required', py('tests/test_page_inspection_ledger.py', 'test_killed_after_claim_before_dispatch_is_unknown_after_restart_and_needs_new_approval')],
    ['worker crash mid-read: unknown, not failed', py('tests/test_page_inspection_ledger.py', 'test_a_worker_that_dies_mid_read_leaves_an_unknown_not_a_failure')],
    ['runtime crash mid-read: recovered as unknown', py('tests/test_page_inspection_ledger.py', 'test_a_runtime_that_dies_mid_read_is_recovered_as_unknown')],
    ['ungrounded answers are refused by the runtime too', py('tests/test_page_inspection_api.py', 'test_an_ungrounded_answer_is_refused')],
    ['stale observation answers are refused', py('tests/test_page_inspection_api.py', 'test_a_repeat_on_the_same_task_is_a_new_action_and_the_old_answer_goes_stale')]
  ],
  // Milestone 7b: one bounded permission, many public pages, one grounded answer.
  research: [
    ['the step vocabulary refuses selectors, scripts and addresses', py('tests/test_research_domain.py', 'test_anything_outside_the_vocabulary_is_refused')],
    ['a search query cannot carry private data out', py('tests/test_research_domain.py', 'test_a_query_that_could_carry_private_data_out_is_refused')],
    ['research reaches public hosts but no forbidden destination', py('tests/test_research_domain.py', 'test_research_may_reach_any_public_host_but_no_forbidden_destination')],
    ['an answer the observations do not support is refused', py('tests/test_research_domain.py', 'test_an_answer_the_observations_do_not_support_is_refused')],
    ['no step runs before the trusted grant', py('tests/test_research_authorization.py', 'test_a_step_under_a_pending_scope_is_refused')],
    ['a step authorization is single-use', py('tests/test_research_authorization.py', 'test_a_step_authorization_is_consumed_once_and_never_again')],
    ['an expired scope authorises nothing', py('tests/test_research_authorization.py', 'test_an_expired_scope_authorises_nothing')],
    ['a revoked scope cannot be revived', py('tests/test_research_authorization.py', 'test_a_revoked_scope_authorises_nothing_and_cannot_be_revived')],
    ['the timeline says authorized, never approved', py('tests/test_research_authorization.py', 'test_the_timeline_says_authorized_not_approved')],
    ['a duplicated planner request executes nothing twice', py('tests/test_research_authorization.py', 'test_replaying_a_request_id_executes_nothing_and_returns_the_stored_step')],
    ['budgets stop the task', py('tests/test_research_authorization.py', 'test_the_step_budget_stops_the_task')],
    ['one session serves the task and keeps its history', py('tests/test_research_browser.py', 'test_a_session_is_reused_across_steps_and_keeps_its_history')],
    ['a link ref is followed without naming an address', py('tests/test_research_browser.py', 'test_a_link_ref_is_followed_without_the_planner_naming_an_address')],
    ['a stale link ref is refused without a request', py('tests/test_research_browser.py', 'test_a_link_ref_from_a_document_the_tab_has_left_is_refused')],
    ['a ref the worker never issued is refused', py('tests/test_research_browser.py', 'test_a_ref_the_worker_never_issued_is_refused')],
    ['the worker applies its own destination policy', py('tests/test_research_browser.py', 'test_the_worker_applies_its_own_destination_policy')],
    ['hostile pages gain no capability and no channel opens', py('tests/test_research_browser.py', 'test_hostile_page_text_is_only_data_and_every_channel_stays_closed')],
    ['mutation requests never leave the browser', py('tests/test_research_browser.py', 'test_same_origin_mutation_requests_never_leave_the_browser')],
    ['a forbidden redirect is never requested', py('tests/test_research_browser.py', 'test_a_redirect_to_a_refused_destination_is_never_requested')],
    ['tabs are task-owned and bounded', py('tests/test_research_browser.py', 'test_tabs_are_task_owned_bounded_and_never_the_last_one')],
    ['multi-hop research ends in a grounded answer', py('tests/test_research_ledger.py', 'test_a_granted_research_task_searches_navigates_follows_and_answers')],
    ['a decoy page cannot ground an answer about the real one', py('tests/test_research_ledger.py', 'test_a_decoy_page_cannot_ground_an_answer_about_the_real_one')],
    ['an unsupported operation is refused with a stable code', py('tests/test_research_ledger.py', 'test_an_operation_outside_the_vocabulary_is_refused_with_a_stable_code')],
    ['the budget stops an endless corridor', py('tests/test_research_ledger.py', 'test_the_step_budget_stops_an_endless_corridor')],
    ['a worker restart makes every semantic ref stale', py('tests/test_research_ledger.py', 'test_a_worker_restart_makes_every_semantic_ref_stale')],
    ['a runtime crash mid-step is unknown, never repeated', py('tests/test_research_ledger.py', 'test_a_runtime_crash_mid_step_leaves_an_unknown_and_no_blind_repeat')],
    ['a planner cannot name an address, a selector or a script', ts('src/main/agent/research-planner.test.ts', 'refuses a reply carrying an extra field')],
    ['an operation that does not exist is refused', ts('src/main/agent/research-planner.test.ts', 'refuses an operation that does not exist')],
    ['an operation outside the confirmed scope is refused', ts('src/main/agent/research-planner.test.ts', 'refuses an operation the confirmed scope does not list')],
    ['a persuaded model cannot record an invented figure', ts('src/main/agent/research-answer.test.ts', 'refuses a persuaded model that invents a figure')],
    ['nothing is searched or opened before the trusted click', ts('src/main/services/agent-research.test.ts', 'is what makes research possible')],
    ['the loop searches, follows a link and grounds its answer', ts('src/main/services/agent-research.test.ts', 'searches, opens a result, follows a link and records a grounded answer')],
    ['a budget stops the loop with an honest answer', ts('src/main/services/agent-research.test.ts', 'stops at the step budget')],
    ['a refused step is re-observed, never repeated', ts('src/main/services/agent-research.test.ts', 're-observes rather than repeating a refused step')],
    ['an unconfirmed step is reported, never repeated', ts('src/main/services/agent-research.test.ts', 'reports an unconfirmed step instead of repeating it')],
    ['links reach the desktop as refs and hosts, never addresses', ts('src/main/services/agent-research.test.ts', 'receives refs, labels and hosts for links')]
  ],
  // The main composer: main decides which single path owns a typed request.
  composer: [
    ['inspection request becomes a card; realtime and open_url never reached', ts('src/renderer/src/composer-routing.test.ts', 'creates the durable inspection and focuses its card')],
    ['typed "yes / approve / go ahead" cannot approve the page', ts('src/renderer/src/composer-routing.test.ts', 'only points at the card')],
    ['request -> card -> click -> one read -> answer in the conversation', ts('src/renderer/src/composer-routing.test.ts', 'request -> card -> trusted click')],
    ['a failed read never falls through to open_url', ts('src/renderer/src/composer-routing.test.ts', 'a failed read is reported and never falls through')],
    ['a refused address is still owned by the agent', ts('src/renderer/src/composer-routing.test.ts', 'a refused address is still owned by the agent')],
    ['ordinary conversation reaches realtime exactly once', ts('src/renderer/src/composer-routing.test.ts', 'with no agent task reaches realtime exactly once')],
    ['appointment requests still reach the durable agent', ts('src/renderer/src/composer-routing.test.ts', 'still go to the durable agent, not realtime')],
    ['unreachable router sends nothing anywhere', ts('src/renderer/src/composer-routing.test.ts', 'if main cannot be asked, nothing is sent anywhere')],
    ['a typed agent request needs no voice session', ts('src/renderer/src/composer-routing.test.ts', 'a page inspection is created with no voice session')],
    ['an unhandled request connects voice, then sends once', ts('src/renderer/src/composer-routing.test.ts', 'an unhandled request connects voice first')],
    ['a voice failure affects only the conversation request', ts('src/renderer/src/composer-routing.test.ts', 'a voice connection failure affects only the conversation request')],
    ['a research request becomes a permission card, not a realtime turn', ts('src/renderer/src/composer-routing-research.test.ts', 'is claimed by the durable agent and shows its permission card')],
    ['research searches only after the trusted Allow click', ts('src/renderer/src/composer-routing-research.test.ts', 'searches and answers only after the trusted Allow click')],
    ['an unconfigured research request is still owned by the agent', ts('src/renderer/src/composer-routing-research.test.ts', 'is still claimed, never passed on, when research is not configured')],
    ['an ordinary question still reaches realtime', ts('src/renderer/src/composer-routing-research.test.ts', 'goes to the realtime conversation')],
    ['typed "yes, go ahead" cannot allow research', ts('src/renderer/src/composer-routing-research.test.ts', 'cannot be allowed by typing')]
  ]
}

function runVitest(files) {
  const output = join(OUT, 'vitest.json')
  spawnSync('npx', ['vitest', 'run', ...files, '--reporter=json', `--outputFile=${output}`], { cwd: ROOT, stdio: 'ignore', shell: true })
  const report = JSON.parse(readFileSync(output, 'utf8'))
  return report.testResults.flatMap((file) => file.assertionResults.map((test) => ({
    file: file.name.replaceAll('\\', '/'),
    name: test.fullName ?? test.title,
    passed: test.status === 'passed'
  })))
}

function runPytest(files) {
  const output = join(OUT, 'pytest.xml')
  spawnSync('uv', ['run', 'pytest', '-p', 'no:cacheprovider', '-q', `--junitxml=${output}`, ...files], { cwd: AGENT, stdio: 'ignore' })
  const xml = readFileSync(output, 'utf8')
  return [...xml.matchAll(/<testcase classname="([^"]+)" name="([^"]+)"[^>]*?(\/>|>([\s\S]*?)<\/testcase>)/g)].map((match) => ({
    file: match[1],
    name: match[2],
    passed: match[3] === '/>' || !/<(failure|error|skipped)/.test(match[4] ?? '')
  }))
}

mkdirSync(OUT, { recursive: true })
const all = Object.entries(CASES).flatMap(([category, cases]) => cases.map(([title, target]) => ({ category, title, ...target })))
const selected = tsOnly ? all.filter((item) => item.runner === 'vitest') : all
const vitest = runVitest([...new Set(selected.filter((item) => item.runner === 'vitest').map((item) => item.file))])
const pytest = tsOnly ? [] : runPytest([...new Set(selected.filter((item) => item.runner === 'pytest').map((item) => item.file))])

const results = selected.map((item) => {
  const pool = item.runner === 'vitest'
    ? vitest.filter((test) => test.file.endsWith(item.file) && test.name.includes(item.test))
    : pytest.filter((test) => item.file.replace(/\.py$/, '').replaceAll('/', '.').endsWith(test.file) && (test.name === item.test || test.name.startsWith(`${item.test}[`)))
  const status = pool.length === 0 ? 'missing' : pool.every((test) => test.passed) ? 'pass' : 'fail'
  return { ...item, status, matched: pool.length }
})

let failed = 0
for (const category of Object.keys(CASES)) {
  const rows = results.filter((row) => row.category === category)
  if (rows.length === 0) continue
  console.log(`\n${category}`)
  for (const row of rows) {
    if (row.status !== 'pass') failed += 1
    console.log(`  ${row.status === 'pass' ? 'PASS' : row.status.toUpperCase().padEnd(4)}  ${row.title}`)
  }
}
const passed = results.filter((row) => row.status === 'pass').length
console.log(`\n${passed}/${results.length} eval cases passed`)
writeFileSync(join(OUT, 'eval-report.json'), `${JSON.stringify({ ranAt: new Date().toISOString(), passed, total: results.length, results }, null, 2)}\n`)
process.exit(failed === 0 ? 0 : 1)
