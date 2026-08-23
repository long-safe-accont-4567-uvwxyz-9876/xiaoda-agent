import { ref, onBeforeUnmount } from 'vue'
import type { MessageApi } from 'naive-ui'
import { api } from '../../../api'
import { t } from '../../../i18n'

export interface VoiceRecordingDeps {
  message: MessageApi
  appendTranscript: (text: string) => void
}

export function useVoiceRecording(deps: VoiceRecordingDeps) {
  const isRecording = ref(false)
  const isTranscribing = ref(false)
  const recordingTime = ref(0)

  let mediaRecorder: MediaRecorder | null = null
  let audioChunks: Blob[] = []
  let recordingTimer: ReturnType<typeof setInterval> | null = null

  async function toggleRecording() {
    if (isTranscribing.value) return // 识别中不允许操作
    if (isRecording.value) {
      stopRecording()
    } else {
      await startRecording()
    }
  }

  async function startRecording() {
    // 非安全上下文（HTTP + 局域网 IP）navigator.mediaDevices 为 undefined，无法录音。
    // 明确检测并提示，避免"点击无反应"。getUserMedia 失败也不再静默吞错。
    const md = navigator.mediaDevices
    if (!md || typeof md.getUserMedia !== 'function') {
      deps.message.error(t('promptInput.voiceUnsupported'))
      return
    }
    try {
      const stream = await md.getUserMedia({ audio: true })
      mediaRecorder = new MediaRecorder(stream)
      audioChunks = []
      mediaRecorder.ondataavailable = (e) => {
        if (e.data.size > 0) audioChunks.push(e.data)
      }
      mediaRecorder.onstop = async () => {
        stream.getTracks().forEach((track) => track.stop())
        const blob = new Blob(audioChunks, { type: 'audio/webm' })
        isTranscribing.value = true
        try {
          const result = await api.speechToText(new File([blob], 'recording.webm', { type: 'audio/webm' }))
          if (result.text) {
            deps.appendTranscript(result.text)
          }
        } catch {
          // 识别失败显示提示，不再静默吞错
          deps.message.error(t('promptInput.voiceFailed'))
        } finally {
          isTranscribing.value = false
          isRecording.value = false
          recordingTime.value = 0
        }
      }
      mediaRecorder.start()
      isRecording.value = true
      recordingTime.value = 0
      recordingTimer = setInterval(() => {
        recordingTime.value++
      }, 1000)
    } catch (e: any) {
      isRecording.value = false
      // 区分常见的麦克风失败原因，给出可操作提示（不再静默失败）
      const name = e?.name || ''
      if (name === 'NotAllowedError') {
        deps.message.error(t('promptInput.voicePermissionDenied'))
      } else if (name === 'SecurityError') {
        // 非安全上下文/页面策略拦截，与用户拒绝授权不同，提示无法开始录音
        deps.message.error(t('promptInput.voiceStartFailed'))
      } else if (name === 'NotFoundError' || name === 'DevicesNotFoundError') {
        deps.message.error(t('promptInput.voiceNoDevice'))
      } else {
        deps.message.error(t('promptInput.voiceStartFailed'))
      }
    }
  }

  function stopRecording() {
    if (mediaRecorder && mediaRecorder.state !== 'inactive') {
      mediaRecorder.stop()
    }
    if (recordingTimer) {
      clearInterval(recordingTimer)
      recordingTimer = null
    }
  }

  onBeforeUnmount(() => {
    if (recordingTimer) clearInterval(recordingTimer)
    if (mediaRecorder && mediaRecorder.state !== 'inactive') {
      mediaRecorder.stop()
    }
  })

  return { isRecording, isTranscribing, recordingTime, toggleRecording }
}
