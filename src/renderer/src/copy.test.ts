import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { COPY, formatFileSize } from './copy'

/**
 * Lints the house voice. These rules come straight from docs/COPY.md, so a
 * string that would confuse or mislead a user fails the build rather than
 * shipping.
 */

/** Every leaf string in COPY, with functions invoked using sample values. */
function collectStrings(value: unknown, path: string[] = []): Array<[string, string]> {
  if (typeof value === 'string') {
    return [[path.join('.'), value]]
  }
  if (typeof value === 'function') {
    // Sample arguments cover the numeric and string shapes used in COPY.
    const sampled = (value as (...args: unknown[]) => unknown)(1240, 3000, '62 MB')
    return typeof sampled === 'string' ? [[path.join('.'), sampled]] : []
  }
  if (Array.isArray(value)) {
    return value.flatMap((entry, index) => collectStrings(entry, [...path, String(index)]))
  }
  if (value && typeof value === 'object') {
    return Object.entries(value).flatMap(([key, entry]) => collectStrings(entry, [...path, key]))
  }
  return []
}

const strings = collectStrings(COPY)

describe('copy inventory', () => {
  it('collects a meaningful number of strings', () => {
    expect(strings.length).toBeGreaterThan(60)
  })
})

describe('banned internal vocabulary', () => {
  // From the banned-terminology table in docs/COPY.md. These describe Lumi's
  // internals and must never reach a user.
  const banned = [
    'IPC',
    'renderer',
    'main process',
    'orchestrator',
    'controller',
    'coordinator',
    'trusted result',
    'fail-closed',
    'fails closed',
    'credential',
    'mock mode',
    'mock voice',
    'LifeLens',
    'pending action',
    'approval ID',
    'embedding',
    'index row',
    'ONNX',
    'CLIP',
    'model pack',
    'magic bytes',
    'stack trace',
    'approved root',
    'canonical path',
    'symlink',
    'junction',
    'sniff'
  ]

  it.each(banned)('never says "%s"', (term) => {
    const offenders = strings.filter(([, text]) => text.toLowerCase().includes(term.toLowerCase()))

    expect(offenders).toEqual([])
  })

  it('never contains a Windows or POSIX absolute path', () => {
    const offenders = strings.filter(([, text]) => /[A-Za-z]:\\|(^|\s)\/(usr|home|Users)\//.test(text))

    expect(offenders).toEqual([])
  })

  it('never contains an HTTP status code or error code', () => {
    const offenders = strings.filter(([, text]) => /\b(?:status|code)\s*\d{3}\b|\bE[A-Z]{3,}\b/.test(text))

    expect(offenders).toEqual([])
  })
})

describe('tone', () => {
  it('never shouts', () => {
    const offenders = strings.filter(([, text]) => text.includes('!'))

    expect(offenders).toEqual([])
  })

  it('never apologises or says "Oops"', () => {
    const offenders = strings.filter(([, text]) => /\b(?:oops|uh oh|sorry)\b/i.test(text))

    expect(offenders).toEqual([])
  })

  it('never blames the user', () => {
    // "You didn't connect Telegram" is the shape being forbidden.
    const offenders = strings.filter(([, text]) => /\byou (?:didn.t|failed|forgot|must)\b/i.test(text))

    expect(offenders).toEqual([])
  })

  /**
   * docs/COPY.md caps copy at two sentences, but several of its own recommended
   * strings run to three. The rule as written and the rule as exemplified
   * disagreed; the doc now states the exception, and this list is its
   * enforcement. A third sentence is allowed only where it carries a distinct
   * recovery step or the reassurance that nothing happened — never to add
   * detail.
   */
  const THIRD_SENTENCE_ALLOWED: Record<string, string> = {
    'voice.microphoneDenied': 'third sentence offers the keep-typing fallback',
    'photos.verificationFailed': 'third sentence is the retry step after "nothing was installed"',
    'voice.demoModeLong': 'settings prose explaining a whole mode',
    'photos.semanticResults': 'states two distinct capability limits the user must know'
  }

  it('stays to two sentences or fewer', () => {
    const offenders = strings.filter(([key, text]) => {
      if (key in THIRD_SENTENCE_ALLOWED) return false
      return (text.match(/[.?!](?:\s|$)/g)?.length ?? 0) > 2
    })

    expect(offenders).toEqual([])
  })

  it('allows no more than three sentences even where a third is permitted', () => {
    const offenders = strings.filter(([, text]) => (text.match(/[.?!](?:\s|$)/g)?.length ?? 0) > 3)

    expect(offenders).toEqual([])
  })
})

describe('honesty', () => {
  it('keeps uncertain Telegram delivery uncertain', () => {
    const text = COPY.telegram.uncertainDelivery

    expect(text).toMatch(/couldn’t confirm/i)
    // It must never round up to success or down to failure.
    expect(text).not.toMatch(/\bsent\b|\bdelivered\b|\bfailed\b/i)
    expect(text).toMatch(/check the chat/i)
  })

  it('says explicitly that nothing happened when something did not happen', () => {
    for (const text of [
      COPY.files.changedBeforeSend,
      COPY.drop.tooLarge('62 MB'),
      COPY.confirm.cancelled,
      COPY.confirm.expired
    ]) {
      expect(text).toMatch(/nothing (?:was|happened|happens)|Lumi stopped/i)
    }
  })

  it('never claims a success in a failure string', () => {
    const failures = [
      COPY.photos.downloadFailed,
      COPY.photos.verificationFailed,
      COPY.files.changedBeforeAction,
      COPY.drop.expired,
      COPY.drop.changed
    ]

    for (const text of failures) {
      expect(text).not.toMatch(/\b(?:succeeded|installed successfully|done)\b/i)
    }
  })

  it('says a discarded download was discarded, not retried', () => {
    expect(COPY.photos.verificationFailed).toMatch(/discarded/i)
    expect(COPY.photos.verificationFailed).toMatch(/nothing was installed/i)
  })

  it('tells the user a dropped file did nothing on its own', () => {
    expect(COPY.drop.addedNote).toMatch(/nothing happens until you choose/i)
    expect(COPY.drop.hoverOneNote).toMatch(/nothing will be sent/i)
    expect(COPY.drop.announce('a.png', 'PNG image', '12 KB')).toMatch(/no action taken/i)
  })

  it('does not imply approved-folder trust for a dropped file', () => {
    expect(COPY.confirm.droppedSource).toMatch(/not an approved folder/i)
  })

  it('confirms that removing a dropped file leaves the original alone', () => {
    expect(COPY.drop.removed).toMatch(/untouched/i)
  })
})

describe('branding', () => {
  it('calls the product Lumi and never LifeLens', () => {
    const offenders = strings.filter(([, text]) => /lifelens/i.test(text))

    expect(offenders).toEqual([])
  })

  it('speaks about Lumi in the third person, never as "I" or "we"', () => {
    const offenders = strings.filter(([, text]) => /\b(?:I|we)\s(?:can|will|couldn|am|are)\b/.test(text))

    expect(offenders).toEqual([])
  })
})

describe('no visible LifeLens anywhere a user can read', () => {
  const sources = [
    'src/renderer/src/LifeLensApp.tsx',
    'src/renderer/src/components/ToolConfirmationCard.tsx',
    'src/renderer/src/components/DroppedFileCard.tsx',
    'src/renderer/src/components/DropOverlay.tsx',
    'src/renderer/src/components/ExplanationCard.tsx',
    'src/renderer/src/components/PhotoResultGrid.tsx',
    'src/renderer/src/error-message.ts',
    'src/renderer/index.html'
  ]

  it.each(sources)('%s contains no user-visible LifeLens text', (relative) => {
    const contents = readFileSync(join(process.cwd(), relative), 'utf8')

    // Quoted text and JSX text nodes are what a user can read. Identifiers such
    // as the LifeLensApp component name and the window.lifeLens bridge are not.
    const visible = contents
      .split('\n')
      .filter((line) => /LifeLens/.test(line))
      .filter((line) => !/import|from '|window\.lifeLens|lifeLens\.|LifeLensApi|export default function|className=|\.tsx?'/.test(line))

    expect(visible).toEqual([])
  })
})

describe('no visible LifeLens in main-authored text', () => {
  const sources = [
    'src/main/index.ts',
    'src/main/services/capture.ts',
    'src/main/services/pending-actions.ts',
    'src/main/services/telegram.ts',
    'src/main/services/tools.ts',
    'src/main/services/dropped-files.ts',
    'src/shared/intent.ts',
    'src/renderer/src/realtime.ts'
  ]

  it.each(sources)('%s raises no LifeLens-branded text', (relative) => {
    const contents = readFileSync(join(process.cwd(), relative), 'utf8')

    /*
     * Internal identifiers are explicitly allowed to keep the original name —
     * the IPC channel prefix, the preload bridge, the appId, and the state
     * filename are not surfaces a user reads. Anything else that mentions
     * LifeLens is text a person could see and must say Lumi.
     */
    const visible = contents
      .split('\n')
      .filter((line) => /LifeLens/i.test(line))
      .filter((line) => !/IPC_CHANNELS|lifelens:|com\.lifelens|lifelens-state|window\.lifeLens|lifeLens\.|LifeLensApi|LifeLensApp|import|from '/.test(line))
      // The self-capture filter must keep matching the old title so a window
      // from an older build is still excluded from the capture source list.
      .filter((line) => !line.includes('(?:lifelens|lumi)'))

    expect(visible).toEqual([])
  })
})

describe('formatFileSize', () => {
  it('reads naturally at each magnitude', () => {
    expect(formatFileSize(512)).toBe('512 B')
    expect(formatFileSize(2048)).toBe('2.0 KB')
    expect(formatFileSize(5 * 1024 * 1024)).toBe('5.0 MB')
  })
})
