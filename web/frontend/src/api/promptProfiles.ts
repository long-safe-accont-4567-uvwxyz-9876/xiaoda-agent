import { get, post } from './index'

export interface PromptProfileSummary {
  prompt_id: string
  version: string
  template_hash: string
  status: 'draft' | 'staging' | 'production' | 'retired'
  staged: boolean
  overridden: boolean
}

export interface GoldenCase {
  case_id: string
  variables: Record<string, string>
  required_fields: string[]
  expect_contains: string[]
  expect_absent: string[]
  evidence_check: boolean
}

export interface PromptProfilesInfo {
  node_id: string
  profiles: PromptProfileSummary[]
  golden_cases: GoldenCase[]
}

export interface AbGate {
  passed: boolean
  reasons: string[]
}

export interface AbOutcome {
  case_id: string
  ok: boolean
  schema_ok: boolean
  missing_fields: string[]
  missed_golds: string[]
  violations: string[]
  bad_quotes: string[]
}

export interface AbSideReport {
  label: string
  schema_rate: number
  golden_rate: number
  violation_count: number
  all_ok: boolean
  outcomes: Record<string, AbOutcome>
}

export interface PromptAbResult {
  prompt_id: string
  node_id: string
  backend: string
  runs?: number
  per_run_candidate_all_ok?: boolean[]
  /** 未 stage 候选时为 true：candidate==baseline，门禁结论视为 pending */
  no_candidate_under_test?: boolean
  report: {
    baseline: AbSideReport
    candidate: AbSideReport
    regressions: string[]
    improvements: string[]
  }
  gate: AbGate
}

export function fetchPromptProfiles(nodeId: string): Promise<PromptProfilesInfo> {
  return get<PromptProfilesInfo>(`/local-deploy/prompt-profiles/${nodeId}`)
}

export interface StagePromptRecord {
  version: string
  system_template?: string
  user_template: string
  variables?: Record<string, { required?: boolean }>
  output_schema?: Record<string, unknown>
}

export function stagePromptProfile(promptId: string, record: StagePromptRecord): Promise<Record<string, unknown>> {
  return post<Record<string, unknown>>(`/local-deploy/prompt-profiles/${promptId}/stage`, record)
}

export function promotePromptProfile(
  promptId: string,
  abReport?: PromptAbResult['report'],
): Promise<{ prompt_id: string; version: string; status: string }> {
  return post(`/local-deploy/prompt-profiles/${promptId}/promote`, abReport ? { ab_report: abReport } : {}, true)
}

export function rollbackPromptProfile(promptId: string): Promise<Record<string, unknown>> {
  return post(`/local-deploy/prompt-profiles/${promptId}/rollback`, undefined, true)
}

export function runPromptAb(
  promptId: string,
  backends?: string[],
  runs?: number,
): Promise<PromptAbResult | PromptAbSweep> {
  const body: Record<string, unknown> = {}
  if (backends && backends.length > 1) body.backends = backends
  if (runs && runs > 1) body.runs = runs
  return post<PromptAbResult | PromptAbSweep>(
    `/local-deploy/prompt-profiles/${promptId}/ab-run`,
    body,
  )
}

export interface PromptAbSweep {
  prompt_id: string
  node_id: string
  backends: string[]
  sweeps: PromptAbResult[]
  gate: AbGate
}

export function isSweep(result: PromptAbResult | PromptAbSweep): result is PromptAbSweep {
  return Array.isArray((result as PromptAbSweep).sweeps)
}
