/**
 * 媒体任务回执提示（审查 M1，高危）。
 *
 * 原缺陷：MediaView.onTaskUpdate 里 `const t = tasks.value.find(...)` 遮蔽了
 * 顶部 `import { t } from '../i18n'`，第 80/83 行再调 t(...) 就是
 * "TypeError: t is not a function"——生成完成与失败都没有提示，成功时
 * loadGallery() 还被跳过。类型检查抓不到是因为 tasks 是 ref<any[]>，
 * find 返回 any，any 可调用。
 *
 * 修法：局部变量改名 task，并把"事件 → 提示"的判定抽成纯函数，
 * 让 translate 作为显式参数传入，任何遮蔽都会变成类型错误而不是运行期炸。
 */

export interface MediaTaskEvent {
  status?: string
  error?: string
}

export interface MediaTaskNotice {
  level: 'success' | 'error'
  text: string
  /** 完成时是否需要刷新画廊 */
  refreshGallery: boolean
}

export function mediaTaskNotice(
  event: MediaTaskEvent,
  translate: (key: string) => string,
): MediaTaskNotice | null {
  if (event.status === 'done') {
    return { level: 'success', text: translate('mediaView.genDone'), refreshGallery: true }
  }
  if (event.status === 'failed') {
    const suffix = event.error ? `：${event.error}` : ''
    return { level: 'error', text: `${translate('mediaView.genFailed')}${suffix}`, refreshGallery: false }
  }
  return null
}
