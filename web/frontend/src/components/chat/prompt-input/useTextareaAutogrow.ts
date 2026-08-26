import { ref, nextTick } from 'vue'

export function useTextareaAutogrow() {
  const textareaRef = ref<HTMLTextAreaElement | null>(null)

  function autoGrow() {
    const el = textareaRef.value
    if (!el) return
    el.style.height = 'auto'
    el.style.height = Math.min(el.scrollHeight, 240) + 'px'
  }

  /** 聚焦输入框（供父组件通过 ref 调用，替代脆弱的 querySelector） */
  function focus() {
    textareaRef.value?.focus()
  }

  function scheduleAutoGrow() {
    nextTick(() => autoGrow())
  }

  return { textareaRef, autoGrow, focus, scheduleAutoGrow }
}
