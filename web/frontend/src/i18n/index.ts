/**
 * 轻量级 i18n 插件
 * - 响应式语言切换（基于 Vue reactive）
 * - 默认中文，支持英文
 * - localStorage 持久化语言偏好
 * - 键索引在模块初始化时一次性摊平（审计 P2-8：t() 每次调用都 split 再逐级下钻，
 *   模板里每行 3–5 次调用，1000 行列表每帧要跑几千次 split；改成 Map 直查后
 *   单次调用只剩一次哈希查找）
 */
import { reactive } from 'vue'
import zh from './zh'
import en from './en'

export type Lang = 'zh' | 'en'
type Dict = Record<string, any>

const dicts: Record<Lang, Dict> = { zh: zh as Dict, en: en as Dict }

/** 把嵌套字典摊平成 "a.b.c" → 值 的只读索引（模块加载时执行一次） */
function flattenDict(dict: Dict): Map<string, any> {
  const index = new Map<string, any>()
  const walk = (node: Record<string, any>, prefix: string): void => {
    for (const key of Object.keys(node)) {
      const value = node[key]
      const path = prefix ? `${prefix}.${key}` : key
      // 只递归普通对象；数组与函数按叶子处理（函数型文案由 tf 调用）
      if (value !== null && typeof value === 'object' && !Array.isArray(value)) {
        walk(value as Record<string, any>, path)
      } else {
        index.set(path, value)
      }
    }
  }
  walk(dict, '')
  return index
}

const indexes: Record<Lang, Map<string, any>> = {
  zh: flattenDict(dicts.zh),
  en: flattenDict(dicts.en),
}

const state = reactive({
  lang: (localStorage.getItem('lang') as Lang) || 'zh',
})

/** 翻译函数 */
function t(key: string): any {
  const localized = indexes[state.lang].get(key)
  if (localized !== undefined) {
    // 函数类型（如 permSwitched）原样返回，由 tf 负责调用
    return typeof localized === 'function' ? localized : String(localized)
  }
  // 回退中文：英文词典缺键时仍显示中文而不是键名
  const fallback = indexes.zh.get(key)
  if (fallback !== undefined) {
    return typeof fallback === 'function' ? fallback : String(fallback)
  }
  return key
}

/** 带参数的翻译 */
function tf(key: string, ...args: any[]): string {
  const result = t(key)
  if (typeof result === 'function') return result(...args)
  return result
}

/** 键是否存在（供 check:i18n 与运行期兜底使用） */
function hasKey(key: string, lang: Lang = state.lang): boolean {
  return indexes[lang].has(key)
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

export { t, tf, hasKey, setLang, getLang, install, state }
export default { install }
