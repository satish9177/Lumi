import {
  MODEL_TASK_CLASSES,
  type ModelDiagnosticView,
  type ModelTaskClass
} from '../../shared/model-contracts'

/**
 * Local, redacted diagnostics for model calls and controller steps.
 *
 * Every entry is built from a closed set of fields whose values are
 * identifiers, enum-like codes or numbers. There is no free-text field: a
 * prompt, a transcript, a form value, a header or a key cannot be recorded
 * because nothing here accepts one. Values that do not look like a code are
 * dropped rather than truncated.
 */

const CODE = /^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,63}$/
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
// Anything shaped like a credential is refused even if it matches CODE.
const SECRET_SHAPE = /(sk-[A-Za-z0-9]|ya29\.|AIza|Bearer|token|secret|key=)/i

export interface DiagnosticInput {
  kind: ModelDiagnosticView['kind']
  taskId?: string
  taskRevision?: number
  provider?: string
  model?: string
  taskClass?: ModelTaskClass
  command?: string
  latencyMs?: number
  inputTokens?: number
  outputTokens?: number
  contextTokens?: number
  result: string
  attempt?: number
}

export interface DiagnosticsSink {
  record(entry: DiagnosticInput): void
}

function code(value: string | undefined): string | undefined {
  return value !== undefined && CODE.test(value) && !SECRET_SHAPE.test(value) ? value : undefined
}

function count(value: number | undefined): number | undefined {
  return value !== undefined && Number.isSafeInteger(value) && value >= 0 && value < 10_000_000 ? value : undefined
}

export function redactDiagnostic(entry: DiagnosticInput, at: Date): ModelDiagnosticView {
  const view: ModelDiagnosticView = { at: at.toISOString(), kind: entry.kind, result: code(entry.result) ?? 'redacted' }
  if (entry.taskId !== undefined && UUID.test(entry.taskId)) view.taskId = entry.taskId
  const revision = count(entry.taskRevision)
  if (revision !== undefined) view.taskRevision = revision
  const provider = code(entry.provider)
  if (provider) view.provider = provider
  const model = code(entry.model)
  if (model) view.model = model
  if (entry.taskClass && MODEL_TASK_CLASSES.includes(entry.taskClass)) view.taskClass = entry.taskClass
  const command = code(entry.command)
  if (command) view.command = command
  for (const field of ['latencyMs', 'inputTokens', 'outputTokens', 'contextTokens', 'attempt'] as const) {
    const value = count(entry[field])
    if (value !== undefined) view[field] = value
  }
  return view
}

export class DiagnosticsLog implements DiagnosticsSink {
  private readonly entries: ModelDiagnosticView[] = []

  constructor(
    private readonly options: { capacity?: number; echo?: boolean; now?: () => number } = {}
  ) {}

  record(entry: DiagnosticInput): void {
    const view = redactDiagnostic(entry, new Date((this.options.now ?? Date.now)()))
    this.entries.push(view)
    const capacity = this.options.capacity ?? 200
    while (this.entries.length > capacity) this.entries.shift()
    // Development only: one structured line, already redacted.
    if (this.options.echo) console.info(`[lumi-diagnostics] ${JSON.stringify(view)}`)
  }

  list(): ModelDiagnosticView[] {
    return this.entries.map((entry) => ({ ...entry }))
  }
}

export const NO_DIAGNOSTICS: DiagnosticsSink = { record: () => undefined }
