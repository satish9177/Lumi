/**
 * Every string a user can read in the renderer.
 *
 * Centralised so the voice can be reviewed and linted in one place, per
 * docs/COPY.md: short, calm, plain, honest about uncertainty, one clear next
 * action, and never blaming the user. Lumi is the actor in any sentence about
 * failure.
 *
 * This module holds *wording only*. No security decision lives here — main
 * still authors every confirmation preview and every bounded failure it raises.
 */

export const COPY = {
  /* ---------------------------------------------------------------- voice */
  voice: {
    connecting: 'Connecting…',
    reconnecting: 'Reconnecting…',
    listening: 'Listening',
    thinking: 'Thinking',
    speaking: 'Speaking',
    ready: 'Ready',
    needsAttention: 'Needs attention',
    offline: 'Lumi is offline. Local file search still works — voice and Telegram will reconnect when the network is back.',
    paused: 'Voice is paused to save battery and quota — ask anything to reconnect.',
    disconnected: 'Voice disconnected. Ask anything and Lumi will reconnect.',
    microphoneUnavailable: 'Lumi can’t reach your microphone. You can keep typing meanwhile.',
    microphoneDenied:
      'Lumi can’t use your microphone. Allow it in Windows Settings → Privacy → Microphone, then tap the mic again. You can keep typing meanwhile.',
    demoModeShort: 'Demo mode',
    demoMode: 'Demo mode — voice and answers are simulated. Everything else works normally.',
    demoModeLong:
      'Demo mode — voice and answers are simulated, so Lumi can be tried without an OpenAI key. Capture, search, confirmation, and Telegram all behave exactly as they will live.',
    keyMissing: 'Lumi’s voice and answers need an OpenAI key, and there isn’t one set up. Everything local still works.',
    live: 'Voice is live. Lumi listens only while the panel is open.'
  },

  /* ------------------------------------------------------------- telegram */
  telegram: {
    disconnected: 'Telegram isn’t connected. Connect it in Settings → Telegram to send messages or files.',
    connecting: 'Connecting to Telegram…',
    qrPrompt: 'Open Telegram on your phone: Settings → Devices → Link Desktop Device, then scan this code. It refreshes automatically.',
    qrPreparing: 'Preparing a Telegram sign-in code…',
    qrExpired: 'That sign-in code expired. Waiting for a fresh one.',
    twoStepRequired: 'Telegram needs your two-step password to finish signing in.',
    recipientNotFound: 'Lumi can’t find that person in Telegram anymore. Search again.',
    recipientRequired: 'Choose one Telegram recipient before sending a file.',
    ambiguousRecipient: 'More than one person matches that name. Choose the one you meant.',
    noRecipients: 'Lumi searches up to ten of your recent chats and contacts, on this device. Nothing is sent to OpenAI.',
    chooseRecipientForAttachment: 'Choose one recipient to continue to the confirmation.',
    captionTooLong: (length: number) => `That caption is ${length} characters. Shorten it to 1024 characters or fewer.`,
    /** Never softened into success, never hardened into failure. */
    uncertainDelivery: 'Lumi couldn’t confirm this message arrived. Check the chat in Telegram before sending it again.',
    sentMessage: 'Sent.',
    sentPhoto: 'Photo sent.',
    sentDocument: 'Document sent.',
    duplicateBlocked: 'Lumi already sent that. Nothing was sent again.',
    connectedNote: 'Recipient names and chat metadata stay on this device.',
    notAffiliated: 'Lumi connects through a third-party Telegram client and is not affiliated with or endorsed by Telegram.'
  },

  /* -------------------------------------------------------- files, search */
  files: {
    folderApprovalRequired:
      'Lumi can only search folders you approve. Ask for a file and Lumi will offer the folder chooser once.',
    folderNotApproved:
      'That file is outside the folders you’ve shared with Lumi. Approve its folder once, and Lumi can search it from then on.',
    missing: 'Lumi can’t find that file anymore. It may have been moved or renamed — search again to refresh the list.',
    changedBeforeSend: 'This file changed after you reviewed it, so nothing was sent. Check it and confirm again.',
    changedBeforeAction: 'This file changed after you reviewed it, so Lumi stopped. Check it and confirm again.',
    noReliableMatch: 'Nothing matched that closely. These are the most recent near-matches — or try different words.',
    nothingMatched: 'Nothing matched that. Try different words, or approve another folder in Settings → Files.',
    filenameFallback: 'No filename matched, so these are the most recent near-matches, newest first.',
    bestMatches: 'Best matches, newest first.',
    revokedFolder: (label: string) => `Lumi no longer searches ${label}.`,
    confirmRevoke: (label: string) => `Stop searching and indexing ${label}?`
  },

  /* --------------------------------------------------------- photo search */
  photos: {
    localOnly:
      'Photos are indexed on this device and are not uploaded. Search by content covers indexed JPEG, PNG, and WebP photos.',
    unavailable: 'Photo search by content isn’t available right now. Lumi can still find photos by name, folder, and date.',
    indexingIncomplete: (done: number, total: number) =>
      `Lumi is still learning your photos (${done.toLocaleString()} of ${total.toLocaleString()}). Results may be incomplete for now — searching by name and date still works fully.`,
    pausedOnBattery: 'Indexing is paused while you’re on battery. It resumes when you plug in.',
    powerUnknown: 'Lumi can’t read the power state, so indexing continues normally.',
    downloadFailed: 'The download stopped before it finished. Nothing was installed — try again when you’re ready.',
    verificationFailed: 'The download didn’t arrive intact, so Lumi discarded it. Nothing was installed. Try again.',
    unsupportedImage: 'Lumi can’t read that image. It may be damaged or in a format Lumi doesn’t handle.',
    semanticResults:
      'These matches came from photo content indexed on this device. Lumi can count visible faces but cannot recognise who someone is. Analysing one photo is a separate confirmed action.',
    nameResults: 'These are name, folder, and date matches. Choose one for the separate confirmed photo analysis.',
    confirmRebuild: 'Clear what Lumi has learned about your photos and start again? You will need to download and index again.',
    confirmDisable: 'Turn off photo search by content? Searching by name and date keeps working.',

    /* ------------------------------------------- Phase 2: text and faces */
    phase2LocalOnly: 'Photo text and visible faces are checked on this device.',
    extrasDownload:
      'Reading text and counting visible faces needs a further 4 MB download, kept on this device.',
    enableTextSearch: 'Find text inside photos',
    enableTextSearchNote: 'Reads words and numbers in screenshots and photographed documents.',
    enableFaceCount: 'Count visible faces',
    // States the limit in the control itself, so the capability is never
    // oversold before someone turns it on.
    enableFaceCountNote: 'Counts how many faces are visible. It cannot tell who anyone is.',
    textProgress: (done: number, total: number) =>
      `Text search: ${done.toLocaleString()} of ${total.toLocaleString()}`,
    faceProgress: (done: number, total: number) =>
      `Face count: ${done.toLocaleString()} of ${total.toLocaleString()}`,
    textReady: 'Text search ready',
    faceReady: 'Face count ready',
    visualReady: 'Visual search ready',
    rebuildTextIndex: 'Rebuild text index',
    rebuildFaceIndex: 'Rebuild face-count index',
    confirmRebuildText: 'Read the text in your photos again? Visual search is not affected.',
    confirmRebuildFaces: 'Count visible faces again? Visual search is not affected.',
    // The answer when someone asks Lumi to find a specific person. It says what
    // Lumi can do instead of only refusing, and the word "yet" is deliberate.
    namedPersonUnsupported: 'Lumi can count visible faces, but it can’t recognise who someone is yet.',
    notCheckedForText: 'Not checked for text yet',
    notCheckedForFaces: 'Not checked for visible faces yet'
  },

  /* --------------------------------------------------------- drag and drop */
  drop: {
    hoverOne: 'Drop to add this file to Lumi.',
    hoverOneNote: 'Nothing will be sent.',
    hoverMany: 'Add one file at a time.',
    tooManyFiles: 'One file at a time, please. Drop a single file and Lumi will pick it up.',
    unsupportedType: 'Lumi can’t take this file type yet. It works with JPEG, PNG, WebP, PDF, Word, and text files.',
    directory: 'Lumi takes one file, not a folder. To let Lumi search a folder, approve it in Settings.',
    shortcut: 'Lumi works with files, not shortcuts. Drop the file itself.',
    virtualFile: 'This file isn’t saved on your computer yet. Save it somewhere first, then drop it on Lumi.',
    tooLarge: (size: string) =>
      `This file is ${size} — Lumi handles files up to 50 MB, and photos up to 10 MB. Nothing was added.`,
    expired: 'That dropped file is no longer available. Drop it again to use it.',
    replaced: 'Lumi keeps one dropped file at a time, so the previous one was let go.',
    changed: 'That file changed after you dropped it, so Lumi stopped. Drop it again to use it.',
    addedNote: 'Added locally. Nothing happens until you choose an action.',
    localOnly: 'Stays on this device',
    documentAnalysisUnavailable:
      'Lumi can open this file or send it on Telegram. Reading its contents isn’t supported yet.',
    removed: 'Removed. The file on your computer is untouched.',
    /** Spoken by a screen reader the moment a file is registered. */
    announce: (name: string, type: string, size: string) => `File added: ${name}, ${type}, ${size}. No action taken.`
  },

  /* ----------------------------------------------------------- confirmation */
  confirm: {
    required: 'Lumi only acts when you confirm. Nothing happens if you cancel.',
    photoLeaves: 'This one photo will be sent to OpenAI so Lumi can answer. No other photo leaves your computer.',
    telegramOnly: 'Only this confirmed file will be sent to Telegram. It is not sent to OpenAI.',
    cancelled: 'Cancelled. Nothing happened.',
    expired: 'That confirmation expired, so nothing happened. Ask again and Lumi will offer it once more.',
    droppedSource: 'Dropped file — temporary, not an approved folder'
  },

  /* --------------------------------------------------------------- capture */
  capture: {
    looking: 'Looking at your screen once to answer this request. Lumi does not save or watch it.',
    failed: 'Lumi couldn’t capture your screen. Try again, or choose a different window.',
    noSources: 'No window or screen is available to capture right now.',
    sourceGone: 'That window is no longer open. Choose another one.'
  },

  /* ---------------------------------------------------------------- window */
  window: {
    positionReset: 'Lumi is back at the bottom-right of your main screen.',
    positionResetFailed: 'Lumi couldn’t move the window just now. Try again.',
    dragHint: 'Drag Lumi by its header to move it. Lumi remembers where you put it.'
  },

  /* ------------------------------------------------------------ empty state */
  empty: {
    greeting: 'Ask about what is on your screen, or find a file.',
    suggestions: ['What is this email about?', 'Find my resume', 'Show me photos of the whiteboard']
  },

  /* -------------------------------------------------------------- controls */
  labels: {
    openLumi: 'Open Lumi',
    collapse: 'Collapse to orb',
    settings: 'Settings',
    closeSettings: 'Close settings',
    ask: 'Ask Lumi',
    send: 'Send',
    captureScreen: 'Look at my screen',
    captureAgain: 'Look at my screen again',
    removeDroppedFile: (name: string) => `Remove ${name}`,
    openDroppedFile: (name: string) => `Open ${name}`,
    analyseDroppedFile: (name: string) => `Analyse ${name}`,
    sendDroppedFile: (name: string) => `Send ${name} on Telegram`,
    /** Explains a disabled control rather than leaving the user guessing. */
    sendDisabledConnecting: 'Lumi is still connecting.',
    sendDisabledEmpty: 'Type a question first.'
  },

  /* --------------------------------------------------------------- generic */
  generic: {
    unexpected: 'Something went wrong and Lumi stopped. Nothing was changed.'
  }
} as const

/** Shared size formatting so every surface reads the same way. */
export function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}
