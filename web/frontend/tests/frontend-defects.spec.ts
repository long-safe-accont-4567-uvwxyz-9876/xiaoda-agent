/**
 * 前端功能审查（2026-11-08）暴露缺陷的回归测试。
 *
 * 覆盖两类东西：
 *  1. 修复用到的纯逻辑（深链白名单、历史分页合并与回滚、倒计时、上传体积、
 *     年月日格式化、焦点环、脏值离开判定、媒体链接卫生、自动播放识别）；
 *  2. 只能用源码扫描才抓得住的缺陷形态（i18n 的 t 被局部变量遮蔽、组件里的
 *     硬编码中文）。
 *
 * 本仓没有 vitest/jsdom，运行器是 scripts/run-unit-tests.mjs（esbuild 打包 +
 * node:test），因此这里不 import .vue，只读它们的源码文本。
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'

import {
  isSafeRedirect,
  resolveRedirectTarget,
  loginLocationWithRedirect,
} from '../src/utils/sessionRedirect'
import { mediaTaskNotice } from '../src/utils/mediaTaskNotice'
import { SAFE_WINDOW_FEATURES, galleryDeletePath, openExternal } from '../src/utils/mediaLinks'
import { hasMoreAfterPage, nextPage } from '../src/utils/paging'
import {
  mergeOlderPage,
  olderCursorSeconds,
  pageExhausted,
  rollbackSessionId,
} from '../src/utils/chatHistory'
import { formatDateTime, formatTime, localeTag } from '../src/utils/datetime'
import {
  CMD_CONFIRM_TIMEOUT_MS,
  confirmPhase,
  confirmRemainingMs,
  formatCountdown,
  pruneExpired,
} from '../src/utils/cmdConfirm'
import { DOC_MAX_BYTES, IMAGE_MAX_BYTES, checkUploadSize, humanSize } from '../src/utils/uploadLimits'
import { nextFocusIndex } from '../src/utils/focusTrap'
import { hasUnsavedChanges, leaveDecision } from '../src/utils/unsavedGuard'
import { isAutoplayBlocked } from '../src/utils/autoplay'
import { describeError, isCancelError } from '../src/utils/errors'
import { hasKey, setLang } from '../src/i18n'

// ── 源码扫描工具 ─────────────────────────────────────────
const SRC = join(process.cwd(), 'src')

function walk(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const full = join(dir, name)
    if (statSync(full).isDirectory()) walk(full, out)
    else if (full.endsWith('.vue')) out.push(full)
  }
  return out
}

/** 去掉注释后再判 CJK / 遮蔽，避免把说明文字当成缺陷 */
function stripComments(src: string): string {
  return src
    .replace(/<!--[\s\S]*?-->/g, '')
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/^\s*\/\/.*$/gm, '')
}

const CJK = /[\u4e00-\u9fff]/

// ── A2 深链回跳 ─────────────────────────────────────────
test('A2: 只放行站内绝对路径，协议相对与外站一律回落', () => {
  assert.equal(resolveRedirectTarget('/media'), '/media')
  assert.equal(resolveRedirectTarget('/insight?tab=graph'), '/insight?tab=graph')
  assert.equal(resolveRedirectTarget('//evil.example.com'), '/')
  assert.equal(resolveRedirectTarget('http://evil.example.com'), '/')
  assert.equal(resolveRedirectTarget('javascript:alert(1)'), '/')
  assert.equal(resolveRedirectTarget('/a\\b'), '/')
  assert.equal(isSafeRedirect(undefined), false)
  // 登录页/引导页自身不能作为回跳目标，否则形成循环
  assert.equal(isSafeRedirect('/login'), false)
  assert.equal(isSafeRedirect('/login?redirect=/media'), false)
  assert.equal(isSafeRedirect('/setup'), false)
})

test('A2: 守卫把目标编码进 login query', () => {
  const loc = loginLocationWithRedirect('/media?type=video')
  assert.equal(loc.name, 'login')
  assert.equal(loc.query.redirect, '/media?type=video')
  assert.equal(loginLocationWithRedirect('//evil.com').query.redirect, '/')
})

// ── M1 媒体任务提示（遮蔽缺陷的行为固化）───────────────────
test('M1: 生成完成给成功提示且要求刷新画廊', () => {
  const calls: string[] = []
  const tr = (k: string) => { calls.push(k); return `[${k}]` }
  assert.deepEqual(mediaTaskNotice({ status: 'done' }, tr),
    { level: 'success', text: '[mediaView.genDone]', refreshGallery: true })
  assert.deepEqual(calls, ['mediaView.genDone'])
})

test('M1: 生成失败带出后端原因；其它状态不提示', () => {
  const tr = (k: string) => `[${k}]`
  const failed = mediaTaskNotice({ status: 'failed', error: 'tts 500' }, tr)
  assert.equal(failed?.level, 'error')
  assert.match(failed?.text || '', /tts 500/)
  assert.equal(failed?.refreshGallery, false)
  assert.equal(mediaTaskNotice({ status: 'failed' }, tr)?.text, '[mediaView.genFailed]')
  assert.equal(mediaTaskNotice({ status: 'running' }, tr), null)
  assert.equal(mediaTaskNotice({}, tr), null)
})

// ── 源码扫描：i18n 的 t 不得被局部变量遮蔽 ──────────────────
test('M1: 没有任何 .vue 用局部 const/let t 遮蔽 i18n 的 t', () => {
  const offenders: string[] = []
  for (const file of walk(SRC)) {
    const raw = readFileSync(file, 'utf-8')
    const importsI18nT = /import\s*\{[^}]*\bt\b[^}]*\}\s*from\s*'[^']*i18n[^']*'/.test(raw)
    if (!importsI18nT) continue
    const body = stripComments(raw)
    if (/\b(?:const|let|var)\s+t\s*[:=]/.test(body)) offenders.push(file)
  }
  assert.deepEqual(offenders, [], `这些文件遮蔽了 i18n 的 t：${offenders.join(', ')}`)
})

// ── M5 媒体链接卫生 ─────────────────────────────────────
test('M5: 画廊删除路径对 type 与 name 双编码', () => {
  assert.equal(galleryDeletePath('image', 'a.png'), '/media/gallery/image/a.png')
  assert.equal(galleryDeletePath('image', 'a b.png'), '/media/gallery/image/a%20b.png')
  // # 不编码会被浏览器当成 fragment，请求打到错误对象上
  assert.equal(galleryDeletePath('video', 'clip#1.mp4'), '/media/gallery/video/clip%231.mp4')
  assert.equal(galleryDeletePath('image', '问?.png'), '/media/gallery/image/%E9%97%AE%3F.png')
})

test('M5: 新窗口打开带 noopener/noreferrer', () => {
  const seen: string[][] = []
  openExternal('https://example.com/a.mp3', (u, target, features) => { seen.push([u, target, features]) })
  assert.deepEqual(seen, [['https://example.com/a.mp3', '_blank', SAFE_WINDOW_FEATURES]])
  assert.match(SAFE_WINDOW_FEATURES, /noopener/)
  assert.match(SAFE_WINDOW_FEATURES, /noreferrer/)
})

// ── M3 分页 ─────────────────────────────────────────────
test('M3: 满页才认为还有下一页，页码单调递增', () => {
  assert.equal(hasMoreAfterPage(48, 48), true)
  assert.equal(hasMoreAfterPage(47, 48), false)
  assert.equal(hasMoreAfterPage(0, 48), false)
  assert.equal(hasMoreAfterPage(5, 0), false)
  assert.equal(nextPage(0), 1)
  assert.equal(nextPage(-1), 0)
})

// ── B1 历史分页 ─────────────────────────────────────────
test('B1: before 游标取最早一条的秒级时间', () => {
  assert.equal(olderCursorSeconds(1_700_000_000_000), 1_700_000_000)
  assert.equal(olderCursorSeconds(0), 0)
  assert.equal(olderCursorSeconds(Number.NaN), 0)
  assert.equal(pageExhausted(50, 50), false)
  assert.equal(pageExhausted(49, 50), true)
})

test('B1: 更早一页合并到头部并按 id 去重', () => {
  const existing = [{ id: 'c' }, { id: 'd' }]
  const merged = mergeOlderPage(existing, [{ id: 'a' }, { id: 'b' }, { id: 'c' }])
  assert.deepEqual(merged.map(m => m.id), ['a', 'b', 'c', 'd'])
  // 全重复时保持原引用，避免无意义重渲染
  assert.equal(mergeOlderPage(existing, [{ id: 'c' }]), existing)
  assert.equal(mergeOlderPage(existing, []), existing)
})

// ── B2 切换会话回滚 ─────────────────────────────────────
test('B2: 只有最近一次切换失败才回滚，迟到请求不回滚', () => {
  assert.equal(rollbackSessionId('s1', 's2', 3, 3), 's1')
  assert.equal(rollbackSessionId('s1', 's2', 2, 3), null, '迟到的旧请求不得顶掉新会话')
  assert.equal(rollbackSessionId('s1', 's1', 3, 3), null)
  assert.equal(rollbackSessionId('', 's2', 3, 3), null)
})

// ── E1 日期跟随语言 ─────────────────────────────────────
test('E1: 时间格式跟随 locale 而不是写死 zh-CN', () => {
  assert.equal(localeTag('zh'), 'zh-CN')
  assert.equal(localeTag('en'), 'en-US')
  const ts = Date.UTC(2026, 10, 8, 14, 30)
  const zh = formatTime(ts, 'zh-CN')
  const en = formatTime(ts, 'en-US')
  assert.notEqual(zh, en)
  assert.match(en, /AM|PM/)
  assert.doesNotMatch(zh, /AM|PM/)
  assert.notEqual(formatDateTime(ts, 'zh-CN'), formatDateTime(ts, 'en-US'))
  assert.ok(formatDateTime(ts).length > 0)
})

// ── G2 命令确认倒计时 ────────────────────────────────────
test('G2: 剩余时间不为负，缺省按完整窗口计', () => {
  assert.equal(confirmRemainingMs(0, 1000), CMD_CONFIRM_TIMEOUT_MS)
  assert.equal(confirmRemainingMs(5000, 9000), 0)
  assert.equal(confirmRemainingMs(9000, 5000), 4000)
})

test('G2: 倒计时向上取整，过期与即将超时可区分', () => {
  assert.equal(formatCountdown(59_000), '0:59')
  assert.equal(formatCountdown(7_200), '0:08')
  assert.equal(formatCountdown(0), '0:00')
  assert.equal(confirmPhase(30_000), 'pending')
  assert.equal(confirmPhase(9_000), 'expiring')
  assert.equal(confirmPhase(0), 'expired')
  const now = 1_000
  assert.deepEqual(pruneExpired([{ deadline: 900 }, { deadline: 2000 }], now), [{ deadline: 2000 }])
})

// ── M2 上传体积 ─────────────────────────────────────────
test('M2: 前后端上限一致（图片 10MB / 文档 20MB）', () => {
  assert.equal(IMAGE_MAX_BYTES, 10 * 1024 * 1024)
  assert.equal(DOC_MAX_BYTES, 20 * 1024 * 1024)
  assert.equal(checkUploadSize(IMAGE_MAX_BYTES, 'image').ok, true)
  assert.deepEqual(checkUploadSize(IMAGE_MAX_BYTES + 1, 'image'),
    { ok: false, code: 'TOO_LARGE', limitBytes: IMAGE_MAX_BYTES })
  assert.equal(checkUploadSize(DOC_MAX_BYTES, 'doc').ok, true)
  assert.equal(checkUploadSize(DOC_MAX_BYTES + 1, 'doc').ok, false)
  assert.equal(checkUploadSize(-1, 'doc').ok, false)
  assert.equal(humanSize(10 * 1024 * 1024), '10MB')
  assert.equal(humanSize(1536 * 1024), '1.5MB')
  assert.equal(humanSize(512 * 1024), '512KB')
})

// ── F1 焦点环 ───────────────────────────────────────────
test('F1: Tab/Shift+Tab 在容器内循环，焦点在环外时从头尾进入', () => {
  assert.equal(nextFocusIndex(0, 3, false), 1)
  assert.equal(nextFocusIndex(2, 3, false), 0)
  assert.equal(nextFocusIndex(0, 3, true), 2)
  assert.equal(nextFocusIndex(1, 3, true), 0)
  assert.equal(nextFocusIndex(-1, 3, false), 0)
  assert.equal(nextFocusIndex(-1, 3, true), 2)
  assert.equal(nextFocusIndex(5, 3, false), 0)
  assert.equal(nextFocusIndex(0, 0, false), -1)
})

// ── C2 未保存离开 ───────────────────────────────────────
test('C2: 脏值判定与 JSON 键序无关，未确认一律拦住', () => {
  assert.equal(hasUnsavedChanges({ a: 1, b: 2 }, { b: 2, a: 1 }), false)
  assert.equal(hasUnsavedChanges({ a: 1 }, { a: 2 }), true)
  assert.equal(hasUnsavedChanges({ a: [1, 2] }, { a: [2, 1] }), true)
  assert.equal(hasUnsavedChanges('', 'x'), true)
  assert.equal(leaveDecision(true, false), 'ask')
  assert.equal(leaveDecision(true, true), 'leave')
  assert.equal(leaveDecision(false, false), 'leave')
})

// ── M4 自动播放 ─────────────────────────────────────────
test('M4: 仅把浏览器的拦截信号识别为自动播放失败', () => {
  assert.equal(isAutoplayBlocked({ name: 'NotAllowedError' }), true)
  assert.equal(isAutoplayBlocked(new Error('play() failed because the user agent requires a user gesture')), true)
  assert.equal(isAutoplayBlocked({ name: 'AbortError' }), false)
  assert.equal(isAutoplayBlocked(null), false)
  assert.equal(isAutoplayBlocked('NotAllowedError'), false)
})

// ── C1 错误文案 ─────────────────────────────────────────
test('C1: 任意抛出物都能取到非空文案，取消不被当成失败', () => {
  assert.equal(describeError(new Error('boom')), 'boom')
  assert.equal(describeError('plain'), 'plain')
  assert.equal(describeError({ message: 'obj' }), 'obj')
  assert.equal(describeError({ code: 'E_X' }), 'E_X')
  assert.equal(describeError(null, 'fallback'), 'fallback')
  assert.equal(isCancelError({ name: 'AbortError' }), true)
  assert.equal(isCancelError(new Error('x')), false)
})

// ── G1 下载面板文案走 i18n ───────────────────────────────
test('G1: DownloadTasksTab 不再有硬编码中文', () => {
  const file = join(SRC, 'components/local-ai/DownloadTasksTab.vue')
  const body = stripComments(readFileSync(file, 'utf-8'))
  const hits = body.match(new RegExp(CJK.source, 'g')) || []
  assert.deepEqual(hits, [], `仍残留 ${hits.length} 个硬编码中文字符`)
})

test('G1/G2: 新增 i18n 键中英齐备', () => {
  const keys = [
    'errors.loadFailed', 'errors.retry', 'errors.historyLoadFailed', 'errors.sessionSwitchFailed',
    'errors.autoplayBlocked', 'errors.uploadTooLarge',
    'common.loadMore', 'common.unsavedLeaveConfirm',
    'chatHistory.loadOlder', 'chatHistory.loadingOlder', 'chatHistory.allLoaded',
    'cmdConfirm.remaining', 'cmdConfirm.expired',
    'downloadTasks.statePending', 'downloadTasks.stateDownloading', 'downloadTasks.statePaused',
    'downloadTasks.stateCompleted', 'downloadTasks.stateFailed', 'downloadTasks.stateCancelled',
    'downloadTasks.stateQuarantined', 'downloadTasks.empty', 'downloadTasks.pause',
    'downloadTasks.resume', 'downloadTasks.cancelDownload', 'downloadTasks.deleteTask',
    'downloadTasks.startModel', 'downloadTasks.confirmTitle',
  ]
  for (const lang of ['zh', 'en'] as const) {
    setLang(lang)
    for (const key of keys) assert.equal(hasKey(key), true, `${lang} 缺少 ${key}`)
  }
  setLang('zh')
})
