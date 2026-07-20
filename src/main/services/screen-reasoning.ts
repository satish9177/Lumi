import { createHash } from 'node:crypto'
import type { ScreenReasoningSummary } from '../../shared/contracts'

export const REASONING_MODEL = process.env.LUMI_REASONING_MODEL?.trim() || 'gpt-5.6-terra'
const REQUEST_TIMEOUT_MS = 30_000
const REASONING_EFFORTS = ['low', 'medium', 'high'] as const
const MAX_SUMMARY_LENGTH = 1_200
const MAX_ITEM_LENGTH = 300
const MAX_ITEMS_PER_SECTION = 8

export interface RetainedCaptureForReasoning {
  id: string
  dataUrl: string
}

type ReasoningEffort = (typeof REASONING_EFFORTS)[number]

const SCREEN_BRIEF_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    summary: { type: 'string', minLength: 1, maxLength: MAX_SUMMARY_LENGTH },
    dates: { type: 'array', maxItems: MAX_ITEMS_PER_SECTION, items: { type: 'string', minLength: 1, maxLength: MAX_ITEM_LENGTH } },
    links: { type: 'array', maxItems: MAX_ITEMS_PER_SECTION, items: { type: 'string', minLength: 1, maxLength: MAX_ITEM_LENGTH } },
    risks: { type: 'array', maxItems: MAX_ITEMS_PER_SECTION, items: { type: 'string', minLength: 1, maxLength: MAX_ITEM_LENGTH } },
    next_actions: { type: 'array', maxItems: MAX_ITEMS_PER_SECTION, items: { type: 'string', minLength: 1, maxLength: MAX_ITEM_LENGTH } }
  },
  required: ['summary', 'dates', 'links', 'risks', 'next_actions']
} as const

export function getReasoningEffort(value = process.env.LUMI_REASONING_EFFORT): ReasoningEffort {
  const normalized = value?.trim().toLowerCase()
  if (!normalized) {
    return 'low'
  }

  if (REASONING_EFFORTS.includes(normalized as ReasoningEffort)) {
    return normalized as ReasoningEffort
  }

  throw new Error(`Invalid LUMI_REASONING_EFFORT. Expected one of: ${REASONING_EFFORTS.join(', ')}.`)
}

export async function createScreenReasoningSummary(
  capture: RetainedCaptureForReasoning,
  safetySeed: string
): Promise<ScreenReasoningSummary> {
  const apiKey = process.env.OPENAI_API_KEY?.trim()
  if (!apiKey) {
    throw new Error('GPT-5.6 screen reasoning needs an OPENAI_API_KEY. Nothing was sent.')
  }

  let response: Response
  try {
    response = await fetchWithTimeout('https://api.openai.com/v1/responses', {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${apiKey}`,
        'Content-Type': 'application/json',
        'OpenAI-Safety-Identifier': createHash('sha256').update(safetySeed).digest('hex')
      },
      body: JSON.stringify({
        model: REASONING_MODEL,
        reasoning: { effort: getReasoningEffort() },
        max_output_tokens: 900,
        store: false,
        input: [
          {
            role: 'developer',
            content: [{
              type: 'input_text',
              text: 'You create a read-only screen brief for Lumi. Use only facts visible in the supplied image. Do not take actions, propose tool calls, ask for credentials, or infer private information. Include only meaningful dates, http/https links, risks or deadlines, and suggested next actions. Leave a list empty when the image does not support it. Do not include local file paths.'
            }]
          },
          {
            role: 'user',
            content: [
              { type: 'input_text', text: 'Summarize this user-approved screen capture.' },
              { type: 'input_image', image_url: capture.dataUrl, detail: 'auto' }
            ]
          }
        ],
        text: {
          format: {
            type: 'json_schema',
            name: 'lumi_screen_brief',
            strict: true,
            schema: SCREEN_BRIEF_SCHEMA
          }
        }
      })
    })
  } catch (error) {
    if (isTimeoutError(error)) {
      throw new Error('GPT-5.6 screen reasoning timed out. Nothing was changed.')
    }
    throw new Error('GPT-5.6 screen reasoning could not reach OpenAI. Nothing was changed.')
  }

  if (!response.ok) {
    throw new Error(`GPT-5.6 screen reasoning was unavailable (status ${response.status}). Nothing was changed.`)
  }

  const responseBody: unknown = await response.json()
  const outputText = extractOutputText(responseBody)
  if (!outputText) {
    throw new Error('GPT-5.6 returned no usable screen brief. Nothing was changed.')
  }

  try {
    return parseScreenReasoningSummary(JSON.parse(outputText), capture.id)
  } catch {
    throw new Error('GPT-5.6 returned an invalid screen brief. Nothing was changed.')
  }
}

export function parseScreenReasoningSummary(value: unknown, sourceCaptureId: string): ScreenReasoningSummary {
  if (!isRecord(value) || !hasOnlyKeys(value, ['summary', 'dates', 'links', 'risks', 'next_actions'])) {
    throw new Error('Screen brief must match the closed schema.')
  }

  const summary = parseText(value.summary, 'summary')
  const dates = parseTextList(value.dates, 'dates')
  const links = parseTextList(value.links, 'links')
  if (links.some((link) => !isSafeUrl(link))) {
    throw new Error('Screen brief links must use http or https.')
  }

  return {
    sourceCaptureId,
    summary,
    dates,
    links,
    risks: parseTextList(value.risks, 'risks'),
    nextActions: parseTextList(value.next_actions, 'next_actions')
  }
}

async function fetchWithTimeout(input: RequestInfo | URL, init: RequestInit): Promise<Response> {
  const controller = new AbortController()
  const timeout = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS)
  try {
    return await fetch(input, { ...init, signal: controller.signal })
  } finally {
    clearTimeout(timeout)
  }
}

function extractOutputText(value: unknown): string | undefined {
  if (!isRecord(value) || !Array.isArray(value.output)) {
    return undefined
  }

  for (const item of value.output) {
    if (!isRecord(item) || item.type !== 'message' || !Array.isArray(item.content)) {
      continue
    }
    for (const content of item.content) {
      if (isRecord(content) && content.type === 'output_text' && typeof content.text === 'string') {
        return content.text
      }
    }
  }

  return undefined
}

function parseText(value: unknown, label: string): string {
  if (typeof value !== 'string') {
    throw new Error(`${label} must be text.`)
  }
  const normalized = value.trim()
  if (normalized.length === 0 || normalized.length > MAX_SUMMARY_LENGTH) {
    throw new Error(`${label} must have a safe length.`)
  }
  return normalized
}

function parseTextList(value: unknown, label: string): string[] {
  if (!Array.isArray(value) || value.length > MAX_ITEMS_PER_SECTION) {
    throw new Error(`${label} must be a short list.`)
  }

  return value.map((item) => {
    if (typeof item !== 'string') {
      throw new Error(`${label} entries must be text.`)
    }
    const normalized = item.trim()
    if (normalized.length === 0 || normalized.length > MAX_ITEM_LENGTH) {
      throw new Error(`${label} entries must have a safe length.`)
    }
    return normalized
  })
}

function isSafeUrl(value: string): boolean {
  try {
    const url = new URL(value)
    return url.protocol === 'https:' || url.protocol === 'http:'
  } catch {
    return false
  }
}

function hasOnlyKeys(value: Record<string, unknown>, expectedKeys: readonly string[]): boolean {
  const keys = Object.keys(value)
  return keys.length === expectedKeys.length && keys.every((key) => expectedKeys.includes(key))
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isTimeoutError(error: unknown): boolean {
  return typeof error === 'object' && error !== null && 'name' in error && error.name === 'AbortError'
}
