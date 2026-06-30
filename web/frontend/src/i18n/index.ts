/**
 * 轻量级 i18n 插件
 * - 响应式语言切换（基于 Vue reactive）
 * - 默认中文，支持英文
 * - localStorage 持久化语言偏好
 */
import { reactive, computed, type ComputedRef } from 'vue'
import zh from './zh'
import en from './en'

export type Lang = 'zh' | 'en'
type Dict = Record<string, any>

const dicts: Record<Lang, Dict> = { zh: zh as Dict, en: en as Dict }

const state = reactive({
  lang: (localStorage.getItem('lang') as Lang) || 'zh',
})

/** 翻译函数 */
function t(key: string): any {
  const dict = dicts[state.lang] as any
  const parts = key.split('.')
  let val: any = dict
  for (const p of parts) {
    val = val?.[p]
    if (val === undefined) {
      // fallback to zh
      val = (dicts.zh as any)
      for (const p2 of parts) {
        val = val?.[p2]
        if (val === undefined) return key
      }
      break
    }
  }
  // 函数类型（如 permSwitched）
  if (typeof val === 'function') return val
  return String(val)
}

/** 带参数的翻译 */
function tf(key: string, ...args: any[]): string {
  const result = t(key)
  if (typeof result === 'function') return result(...args)
  return result
}

/** 切换语言 */
function setLang(lang: Lang) {
  state.lang = lang
  localStorage.setItem('lang', lang)
}

/** 获取当前语言 */
function getLang(): Lang {
  return state.lang
}

/** Vue 插件安装 */
function install(app: any) {
  app.config.globalProperties.$t = t
  app.config.globalProperties.$tf = tf
  app.config.globalProperties.$lang = state
  app.provide('i18n', { t, tf, setLang, getLang, state })
}

export { t, tf, setLang, getLang, install, state }
export default { install }
