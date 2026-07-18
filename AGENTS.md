# Lumi Agent Instructions

- Build continuously toward the complete hero scenario.
- Prefer working functionality over unnecessary abstraction.
- Never expose API keys in renderer or committed files.
- Run typecheck, tests, and build after significant changes.
- Do not implement features outside the agreed MVP.
- Treat Electron main and preload as the security boundary. Renderer code must use only the typed `window.lifeLens` bridge; it must never use Node, Electron, a permanent API key, or a generic IPC channel.
- All state-changing or external operations require an explicit renderer confirmation before the main process executes them. Validate every IPC payload in the main process.
- Keep screen capture user-initiated. Search only paths saved as user-approved roots.
- Before parallel work, the primary agent owns `src/shared`, root configuration, `src/main`, `src/preload`, and integration. Assign non-overlapping folder ownership and use isolated worktrees for any subagent work.
- The verified commands are `npm.cmd run typecheck`, `npm.cmd test`, `npm.cmd run build`, and `npm.cmd run package`.
