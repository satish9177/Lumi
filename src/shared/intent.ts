// Structured intent layer for tool routing. Classification is semantic: small
// category vocabularies (verbs, nouns, deictic markers) combine into one coarse
// intent, which the deterministic tool-policy guard uses at the capability
// boundary. It is intentionally not a per-sentence phrase dictionary.

export type UserIntent =
  | 'local_file_search'
  | 'visible_screen_question'
  /** "Is this a scam?" — a narrower case of a visible-screen question. */
  | 'scam_check'
  | 'reminder'
  | 'open_target'
  | 'general_question'
  | 'unknown'

export interface IntentContext {
  /** True when a recent screen capture is already available for follow-ups. */
  hasScreenContext?: boolean
}

export interface ClassifiedIntent {
  normalizedRequest: string
  intent: UserIntent
  /** The stored-file noun that anchored a local_file_search classification. */
  fileQuery?: string
  /** Exact question to ask when the request is ambiguous between a visible and a stored document. */
  clarification?: string
}

export const GUARDED_TOOLS = ['capture_screen_context', 'search_documents'] as const
export type GuardedTool = (typeof GUARDED_TOOLS)[number]

export type ToolPolicyCode = 'use_search_documents' | 'needs_approved_folder'

export type ToolPolicyDecision =
  | { allowed: true }
  | { allowed: false; code: ToolPolicyCode; message: string }

const STORED_FILE_NOUN = /\b(resumes?|cvs?|pdfs?|documents?|docs?|files?|folders?|certificates?|spreadsheets?|presentations?|images?|photos?|screenshots?|pictures?|pics?)\b/
// Stored image nouns are checked before the screen reference so that "find my
// newest screen shot" searches saved files instead of capturing the display.
const STORED_IMAGE_NOUN = /\b(screen\s?shots?|screen\s?captures?|screengrabs?|screenshots?|photos?|pictures?|pics?|images?)\b/
const RETRIEVAL_VERB = /\b(find|locate|search|open|fetch|retrieve|get)\b|\bwhere\s+(?:is|are)\b/
const AMBIGUOUS_DOCUMENT_VERB = /\b(?:check|review)\b/
const SCREEN_REFERENCE = /\b(?:screen|monitor|display(?:ed)?|visible|showing)\b/
const DEICTIC_REFERENCE = /\b(?:this|these|here)\b/
const REMINDER_CUE = /\bremind\w*\b|\bremember\s+to\b/
const LINK_TARGET = /\bhttps?:\/\/|\bwww\.|\b(?:link|url|website|webpage|site)\b|\b[a-z0-9-]+\.(?:com|org|net|io|dev|ai)\b/
const OPEN_TARGET_VERB = /\b(?:open|visit|launch)\b|\bgo\s+to\b/
// Deliberately narrow. A scam cue on its own is not enough — the request must
// also point at something visible or name a kind of message — so that
// "remind me about that scam call" stays a reminder and a general question
// about scams stays a general question.
const SCAM_CUE = /\bscam\w*\b|\bfraud\w*\b|\bphish\w*\b|\b(?:suspicious|spoofed?|impersonat\w+)\b|\bcan\s+i\s+trust\b|\bis\s+(?:this|it)\s+(?:\w+\s+)?(?:real|genuine|legit|legitimate|safe)\b/
const MESSAGE_NOUN = /\b(?:message|messages|email|emails|mail|sms|texts?|whats\s?app|payment|link|url|request|invoice|otp|sender|caller|call)\b/

export function normalizeUserRequest(request: string): string {
  return request.trim().replace(/\s+/g, ' ')
}

export function classifyUserIntent(request: string, context: IntentContext = {}): ClassifiedIntent {
  const normalizedRequest = normalizeUserRequest(request)
  const text = normalizedRequest.toLowerCase()
  if (!text) {
    return { normalizedRequest, intent: 'unknown' }
  }

  if (REMINDER_CUE.test(text)) {
    return { normalizedRequest, intent: 'reminder' }
  }

  if (OPEN_TARGET_VERB.test(text) && LINK_TARGET.test(text)) {
    return { normalizedRequest, intent: 'open_target' }
  }

  // Checked before the screen and stored-file rules so that "is this email a
  // scam?" reaches the scam preset rather than the generic screen brief. It
  // still resolves to a screen capture behind the existing confirmation; the
  // only thing that changes is which review the user is offered.
  if (SCAM_CUE.test(text) && (DEICTIC_REFERENCE.test(text) || SCREEN_REFERENCE.test(text) || MESSAGE_NOUN.test(text))) {
    return { normalizedRequest, intent: 'scam_check' }
  }

  // "Find my newest screenshot" is a stored-file request, even though it names
  // the screen. A deictic word such as "this" still means the visible screen.
  const storedImageNoun = STORED_IMAGE_NOUN.exec(text)?.[1]
  if (storedImageNoun && RETRIEVAL_VERB.test(text) && !DEICTIC_REFERENCE.test(text)) {
    return { normalizedRequest, intent: 'local_file_search', fileQuery: storedImageNoun }
  }

  if (SCREEN_REFERENCE.test(text) || DEICTIC_REFERENCE.test(text)) {
    return { normalizedRequest, intent: 'visible_screen_question' }
  }

  const storedFileNoun = STORED_FILE_NOUN.exec(text)?.[1]
  if (storedFileNoun && RETRIEVAL_VERB.test(text)) {
    return { normalizedRequest, intent: 'local_file_search', fileQuery: storedFileNoun }
  }

  if (storedFileNoun && AMBIGUOUS_DOCUMENT_VERB.test(text)) {
    if (context.hasScreenContext) {
      return { normalizedRequest, intent: 'visible_screen_question' }
    }
    return {
      normalizedRequest,
      intent: 'unknown',
      clarification: `Should I inspect the ${storedFileNoun} currently visible, or find it in your approved folder?`
    }
  }

  return { normalizedRequest, intent: 'general_question' }
}

export interface GuardedToolState {
  intent: UserIntent
  hasApprovedFolder: boolean
}

export function evaluateGuardedToolRequest(toolName: GuardedTool, state: GuardedToolState): ToolPolicyDecision {
  if (toolName === 'capture_screen_context') {
    if (state.intent === 'local_file_search') {
      return {
        allowed: false,
        code: 'use_search_documents',
        message: 'The user asked to find a stored file. Screen capture is not a fallback for local file search; call search_documents with an approved folder identifier instead.'
      }
    }
    return { allowed: true }
  }

  if (!state.hasApprovedFolder) {
    return {
      allowed: false,
      code: 'needs_approved_folder',
      message: 'No folder is approved for document search yet. Lumi is asking the user to approve a folder; call search_documents again once one is approved.'
    }
  }
  return { allowed: true }
}
