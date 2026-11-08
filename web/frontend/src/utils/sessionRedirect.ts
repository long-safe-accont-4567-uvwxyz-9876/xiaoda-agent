/**
 * 未登录深链回跳（审查 A2）。
 *
 * 原实现：main.ts 守卫 `next({ name: 'login' })` 不带 redirect，LoginView 登录后
 * 无条件 `replace('/')`——从 /media 这类深链被踢回登录页后，回来必然落到首页。
 * api/index.ts 的 handleUnauthorized（401 统一清理）是同根因的第二条路径。
 *
 * 这里把"目标是否安全可回跳"做成纯函数：只接受站内绝对路径，挡掉协议相对
 * URL（//evil.com）、外站与登录/引导页自身（避免死循环）。
 */

const SELF_PATHS = ['/login', '/setup', '/setup/profile']

export function isSafeRedirect(target: unknown): target is string {
  if (typeof target !== 'string') return false
  const path = target.trim()
  if (!path.startsWith('/')) return false
  if (path.startsWith('//')) return false            // 协议相对 URL → 外站
  if (path.includes('\\')) return false              // 反斜杠会被部分浏览器归一成 /
  if (/^\/\s*$/.test(path)) return true              // 根路径本身合法
  const base = path.split('?')[0].split('#')[0]
  return !SELF_PATHS.includes(base)
}

/** 解析出可以安全回跳的目标；任何不安全输入都回落到 fallback */
export function resolveRedirectTarget(raw: unknown, fallback = '/'): string {
  return isSafeRedirect(raw) ? raw : fallback
}

/** 把当前访问目标编码进登录页 query（供守卫使用） */
export function loginLocationWithRedirect(fullPath: string): { name: string; query: { redirect: string } } {
  const target = resolveRedirectTarget(fullPath)
  return { name: 'login', query: { redirect: target } }
}
