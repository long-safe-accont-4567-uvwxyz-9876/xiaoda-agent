import MarkdownIt from 'markdown-it'
import hljs from 'highlight.js/lib/core'
import javascript from 'highlight.js/lib/languages/javascript'
import typescript from 'highlight.js/lib/languages/typescript'
import python from 'highlight.js/lib/languages/python'
import json from 'highlight.js/lib/languages/json'
import bash from 'highlight.js/lib/languages/bash'
import xml from 'highlight.js/lib/languages/xml'
import css from 'highlight.js/lib/languages/css'
import sql from 'highlight.js/lib/languages/sql'
import 'highlight.js/styles/atom-one-dark.css'

hljs.registerLanguage('javascript', javascript)
hljs.registerLanguage('typescript', typescript)
hljs.registerLanguage('python', python)
hljs.registerLanguage('json', json)
hljs.registerLanguage('bash', bash)
hljs.registerLanguage('xml', xml)
hljs.registerLanguage('html', xml)
hljs.registerLanguage('css', css)
hljs.registerLanguage('sql', sql)

const md = new MarkdownIt({
  html: false,
  linkify: true,
  breaks: true,
  highlight(code: string, lang: string): string {
    if (lang && hljs.getLanguage(lang)) {
      try {
        return `<pre class="hljs"><code>${hljs.highlight(code, { language: lang }).value}</code></pre>`
      } catch { /* fall through */ }
    }
    return `<pre class="hljs"><code>${md.utils.escapeHtml(code)}</code></pre>`
  },
})

// --- LRU 缓存层 ---
// 每个 token 都会 mutate msg.content 触发 v-html 重渲染，无缓存时 markdown-it
// 从零解析整段 + highlight.js 高亮，复杂度 O(n²)。按 text 内容缓存渲染结果。
const MAX_CACHE_ENTRIES = 100
const MAX_ENTRY_BYTES = 50 * 1024 // 50KB：超出不缓存，避免单条占用过大
const renderCache = new Map<string, string>()
const encoder = new TextEncoder()

export function renderMarkdown(text: string): string {
  const input = text || ''
  const cached = renderCache.get(input)
  if (cached !== undefined) {
    // 命中：移到末尾（最近使用），Map 按插入序迭代实现 LRU
    renderCache.delete(input)
    renderCache.set(input, cached)
    return cached
  }
  const result = md.render(input)
  // 仅缓存 50KB 以内的输入，防止内存膨胀
  if (encoder.encode(input).length <= MAX_ENTRY_BYTES) {
    if (renderCache.size >= MAX_CACHE_ENTRIES) {
      const oldest = renderCache.keys().next().value
      if (oldest !== undefined) renderCache.delete(oldest)
    }
    renderCache.set(input, result)
  }
  return result
}

/** 切换会话时清理缓存，避免旧条目堆积 */
export function clearMarkdownCache(): void {
  renderCache.clear()
}
