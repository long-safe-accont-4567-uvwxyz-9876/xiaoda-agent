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
  /** 近期审计日志条目（内存环形缓冲，重启清空） */
  const auditLog = ref<Array<{
    timestamp: string
    action: string
    target: string
    cwd: string
    allowed: boolean
    reason: string
  }>>([])
  /** 待确认的命令请求（由工具 needs_confirmation 状态触发） */
  const pendingCmdConfirm = ref<{ request_id: string; command: string; session_id: string } | null>(null)
  /** 已授权目录列表（localStorage 持久化：曾授权且未在设置页撤销的目录）
   *  选这些目录时直接切换，不再弹授权确认。后端 confirm 总会重新授权，故前端记录即可。 */
  const authorizedDirs = ref<string[]>(JSON.parse(localStorage.getItem('ws.authorizedDirs') || '[]'))
  function _saveAuthorizedDirs() {
    localStorage.setItem('ws.authorizedDirs', JSON.stringify(authorizedDirs.value))
  }

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
    // 记录到已授权目录列表（去重），后续切换该目录无需再弹授权确认
    if (path && !authorizedDirs.value.includes(path)) {
      authorizedDirs.value.push(path)
      _saveAuthorizedDirs()
    }
    // CodeRabbit #4：授权上下文变更后清空旧审计日志，避免展示与新目录无关的陈旧条目
    auditLog.value = []
    return data
  }

  /** 撤销授权 */
  async function revoke() {
    const removed = currentPath.value
    await del('/workspace', true)
    authorized.value = false
    currentPath.value = ''
    authorizedAt.value = ''
    // 从已授权目录列表移除：再次进入该目录需重新授权
    if (removed) {
      authorizedDirs.value = authorizedDirs.value.filter(p => p !== removed)
      _saveAuthorizedDirs()
    }
    // CodeRabbit #4：撤销后旧审计日志不再相关，清空
    auditLog.value = []
  }

  /** 添加命令到白名单 */
  async function addWhitelist(command: string) {
    const data = await post<any>('/workspace/whitelist', { command })
    whitelist.value = data.whitelist
  }

  /** 从白名单删除命令 */
  async function removeWhitelist(command: string) {
    const data = await del<any>(`/workspace/whitelist/${encodeURIComponent(command)}`, true)
    whitelist.value = data.whitelist
  }

  /** 浏览目录（返回子目录列表） */
  async function browse(path: string): Promise<{ current: string; parent: string | null; dirs: string[] }> {
    const q = path ? `?path=${encodeURIComponent(path)}` : ''
    return await get<any>(`/workspace/browse${q}`)
  }

  /** 加载审计日志（限制条数，默认 100） */
  async function loadAudit(limit: number = 100) {
    try {
      const data = await get<any>(`/workspace/audit?limit=${limit}`)
      auditLog.value = data.entries || []
    } catch {
      // CodeRabbit #4：加载失败时清空，避免残留陈旧条目被误认为当前状态
      auditLog.value = []
    }
  }

  /** 回传命令确认决策 */
  async function confirmCmd(
    requestId: string,
    decision: 'allow' | 'allow_once' | 'deny',
    addToWhitelist: boolean,
    command: string = '',
    sessionId: string = '',
  ) {
    await post('/workspace/confirm_cmd', {
      request_id: requestId,
      decision,
      add_to_whitelist: addToWhitelist,
      command,
      session_id: sessionId,
    })
    pendingCmdConfirm.value = null
  }

  return {
    authorized, currentPath, authorizedAt, whitelist, auditLog, pendingCmdConfirm, authorizedDirs,
    loadStatus, loadWhitelist, loadAudit, confirm, revoke,
    addWhitelist, removeWhitelist, browse, confirmCmd,
  }
})
