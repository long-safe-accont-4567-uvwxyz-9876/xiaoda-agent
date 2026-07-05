/**
 * 全局 Agent 名称替换工具
 *
 * 后端 /api/v1/agent-names 返回 { 原名: 显示名 } 映射表，
 * 前端所有显示 agent 名称的地方统一调用 replaceAgentNames()。
 * 改一次 display_name → 全项目 90% 自动同步。
 */

import { get } from '../api'

let _mapping: Record<string, string> = {}
let _loaded = false

/** 从后端加载名称映射表（应用启动时调用一次） */
export async function loadAgentNames(): Promise<void> {
  try {
    const data = await get<{ mapping: Record<string, string> }>('/agent-names')
    _mapping = data.mapping || {}
    _loaded = true
  } catch {
    // 降级：不替换，使用原名
    _loaded = true
  }
}

/** 是否已加载 */
export function isAgentNamesLoaded(): boolean {
  return _loaded
}

/** 获取当前映射表（只读） */
export function getAgentNamesMapping(): Readonly<Record<string, string>> {
  return _mapping
}

/**
 * 替换文本中的 agent 原名为显示名。
 * 按原名长度降序替换，避免短名破坏长名。
 * 例：replaceAgentNames("可莉（klee）") → "小莉（Xiaoli）"
 */
export function replaceAgentNames(text: string): string {
  if (!text || !_loaded) return text
  // 按 key 长度降序排列，避免短名破坏长名
  const sorted = Object.entries(_mapping).sort((a, b) => b[0].length - a[0].length)
  for (const [original, display] of sorted) {
    if (original !== display) {
      text = text.split(original).join(display)
    }
  }
  return text
}

/** 刷新映射表（display_name 变更后调用） */
export async function refreshAgentNames(): Promise<void> {
  _loaded = false
  await loadAgentNames()
}
