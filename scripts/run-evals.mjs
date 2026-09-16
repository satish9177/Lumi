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
