import type { AgentResult, AgentTaskStatus } from './agent-contracts'

/**
 * Milestone 10 S3 renderer-safe shapes: registered projects, trusted recipes and supervised runs.
 *
 * Nothing here carries a path, a pid, an executable location or an environment VALUE. A project is an id
 * and a label; a recipe shows the script name and text the project's package.json declares (so the person
 * can see exactly what will run) and only the NAMES of its environment variables. The log tail is
 * untrusted output, shown as inert text and never sent anywhere.
 */

export const RUN_WARNING = 'This recipe executes code from this project with your user-level permissions.'

export const PROJECT_RUN_PHASES = [
  'awaiting_approval', 'approved', 'declined', 'expired', 'starting', 'running', 'ready', 'succeeded', 'failed',
  'stopped', 'ended_with_runtime', 'outcome_unknown'
] as const
export type AgentProjectRunPhase = typeof PROJECT_RUN_PHASES[number]

export interface AgentProjectView {
  projectId: string
  label: string
  revision: number
  createdAt: string
}

export interface AgentProjectScriptView {
  name: string
  text: string
}

export interface AgentProjectRecipeView {
  recipeId: string
  projectId: string
  label: string
  script: string
  scriptText: string
  preText?: string
  postText?: string
  envNames: string[]
  readinessKind: 'http' | 'exit_code'
  readyPort?: number
  readyPath?: string
  timeoutSeconds: number
  status: 'ACTIVE' | 'INVALIDATED' | 'REVOKED'
  invalidReason?: string
  revision: number
}

export interface AgentProjectRunCardView {
  grantId: string
  grantRevision: number
  grantStatus: 'PENDING' | 'ACTIVE' | 'REVOKED' | 'EXPIRED' | 'COMPLETED'
  expiresAt?: string
  recipeId: string
  recipeRevision: number
  projectLabel: string
  label: string
  script: string
  scriptText: string
  preText?: string
  postText?: string
  argv: string[]
  envNames: string[]
  readinessKind: 'http' | 'exit_code'
  readyPort?: number
  readyPath?: string
  timeoutSeconds: number
  stopPolicy: 'terminate_job'
  warning: typeof RUN_WARNING
}

export interface AgentProjectRunView {
  taskId: string
  taskStatus: AgentTaskStatus
  taskRevision: number
  phase: AgentProjectRunPhase
  startStatus?: string
  errorCode?: string
  exitCode?: number
  activeProcesses: number
  ready: boolean
  logTail: string[]
  card?: AgentProjectRunCardView
}

export interface AgentProjectRecipeInput {
  label: string
  script: string
  readinessKind: 'http' | 'exit_code'
  readyPort?: number
  readyPath?: string
  timeoutSeconds: number
  env: Record<string, string>
}

/** The M10 S3 bridge methods. Ids, revisions, labels and a closed recipe form -- never a path or a command. */
export interface AgentProjectApi {
  listProjects: () => Promise<AgentResult<AgentProjectView[]>>
  /** Main opens a NATIVE folder dialog, then a native confirmation with the execution warning. */
  addProject: (label: string) => Promise<AgentResult<AgentProjectView | null>>
  revokeProject: (projectId: string, expectedRevision: number) => Promise<AgentResult<AgentProjectView>>
  listProjectScripts: (projectId: string) => Promise<AgentResult<AgentProjectScriptView[]>>
  /** Main shows the exact script text in a native confirmation before registering the recipe. */
  createProjectRecipe: (projectId: string, recipe: AgentProjectRecipeInput) => Promise<AgentResult<AgentProjectRecipeView | null>>
  listProjectRecipes: () => Promise<AgentResult<AgentProjectRecipeView[]>>
  revokeProjectRecipe: (recipeId: string, expectedRevision: number) => Promise<AgentResult<AgentProjectRecipeView>>
  /** Opens the per-run card. Nothing runs. */
  createProjectRun: (recipeId: string) => Promise<AgentResult<AgentProjectRunView>>
  getProjectRun: (taskId: string) => Promise<AgentResult<AgentProjectRunView>>
  getLatestProjectRun: () => Promise<AgentResult<AgentProjectRunView | null>>
  /** The trusted approval (R3), re-confirmed by main's native dialog with the execution warning. */
  grantProjectRun: (taskId: string, grantId: string, expectedRevision: number) => Promise<AgentResult<AgentProjectRunView | null>>
  declineProjectRun: (taskId: string, grantId: string, expectedRevision: number) => Promise<AgentResult<AgentProjectRunView>>
  startProjectRun: (taskId: string) => Promise<AgentResult<AgentProjectRunView>>
  /** Ends this run's own process tree, and only it. */
  stopProjectRun: (taskId: string) => Promise<AgentResult<AgentProjectRunView>>
  reconcileProjectRun: (taskId: string) => Promise<AgentResult<AgentProjectRunView>>
}
