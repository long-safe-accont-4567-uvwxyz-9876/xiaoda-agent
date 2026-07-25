import { defineStore } from 'pinia'
import { ref } from 'vue'
import { get, post, del } from '../api'

/**
 * 工作目录授权 Pinia store
 *
 * 管理当前工作目录授权状态、命令白名单。
 * 后端为权威源，前端缓存用于 UI 展示。
 */
export const useWorkspaceStore = defineStore('workspace', () => {
  const authorized = ref(false)
  const currentPath = ref('')
  const authorizedAt = ref('')
  const whitelist = ref<string[]>([])
  /** 待确认的命令请求（由工具 needs_confirmation 状态触发） */
  const pendingCmdConfirm = ref<{ request_id: string; command: string } | null>(null)

  /** 加载当前授权状态（从后端） */
  async function loadStatus() {
    try {
      const data = await get<any>('/workspace')
      authorized.value = data.authorized
      currentPath.value = data.path || ''
      authorizedAt.value = data.authorized_at || ''
    } catch { /* 未登录时静默 */ }
  }

  /** 加载命令白名单 */
  async function loadWhitelist() {
    try {
      const data = await get<any>('/workspace/whitelist')
      whitelist.value = data.whitelist || []
    } catch { /* 忽略 */ }
  }

  /** 确认授权工作目录 */
  async function confirm(path: string) {
    const data = await post<any>('/workspace/confirm', { path })
    authorized.value = data.authorized
    currentPath.value = data.path
    authorizedAt.value = data.authorized_at
    return data
  }

  /** 撤销授权 */
  async function revoke() {
    await del('/workspace')
    authorized.value = false
    currentPath.value = ''
    authorizedAt.value = ''
  }

  /** 添加命令到白名单 */
  async function addWhitelist(command: string) {
    const data = await post<any>('/workspace/whitelist', { command })
    whitelist.value = data.whitelist
  }

  /** 从白名单删除命令 */
  async function removeWhitelist(command: string) {
    const data = await del<any>(`/workspace/whitelist/${encodeURIComponent(command)}`)
    whitelist.value = data.whitelist
  }

  /** 浏览目录（返回子目录列表） */
  async function browse(path: string): Promise<{ current: string; parent: string | null; dirs: string[] }> {
    const q = path ? `?path=${encodeURIComponent(path)}` : ''
    return await get<any>(`/workspace/browse${q}`)
  }

  /** 回传命令确认决策 */
  async function confirmCmd(
    requestId: string,
    decision: 'allow' | 'allow_once' | 'deny',
    addToWhitelist: boolean,
    command: string = '',
  ) {
    await post('/workspace/confirm_cmd', {
      request_id: requestId,
      decision,
      add_to_whitelist: addToWhitelist,
      command,
    })
    pendingCmdConfirm.value = null
  }

  return {
    authorized, currentPath, authorizedAt, whitelist, pendingCmdConfirm,
    loadStatus, loadWhitelist, confirm, revoke,
    addWhitelist, removeWhitelist, browse, confirmCmd,
  }
})
