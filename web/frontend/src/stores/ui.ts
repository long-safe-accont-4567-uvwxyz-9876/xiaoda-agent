import { defineStore } from 'pinia'
import { ref, watch } from 'vue'
import { get, put } from '../api'
import { sound } from '../utils/sound'

export type ParticleDensity = 'off' | 'low' | 'medium' | 'high'

/** 根据小时计算自动亮度（6-18: 1.15, 18-22: 0.95, 22-6: 0.7） */
function autoBrightnessByHour(): number {
  const h = new Date().getHours()
  if (h >= 6 && h < 18) return 1.15      // 白天：明亮
  if (h >= 18 && h < 22) return 0.95     // 傍晚：标准
  return 0.7                              // 夜间：护眼
}

const STORAGE_KEY = 'ui.brightness'
const AUTO_KEY = 'ui.autoBrightness'

export const useUiStore = defineStore('ui', () => {
  const particles = ref<ParticleDensity>(
    (localStorage.getItem('ui.particles') as ParticleDensity) || 'medium')
  const tilt3d = ref(localStorage.getItem('ui.tilt3d') !== 'false')
  const autoSpeak = ref(false)
  const loaded = ref(false)
  // 草元素音效：开关与音量（引擎内部已持久化到 localStorage）
  const soundFx = ref(sound.isEnabled())
  const soundVolume = ref(sound.getVolume())

  // 亮度控制：0.5 (暗) ~ 1.5 (亮)，默认 1.05（比原来稍亮）
  const autoBrightness = ref(localStorage.getItem(AUTO_KEY) !== 'false') // 默认开启自动
  const manualBrightness = ref(parseFloat(localStorage.getItem(STORAGE_KEY) || '1.05'))
  const brightness = ref(autoBrightness.value ? autoBrightnessByHour() : manualBrightness.value)

  /** 应用亮度到 CSS 变量 */
  function applyBrightness() {
    const val = autoBrightness.value ? autoBrightnessByHour() : manualBrightness.value
    brightness.value = val
    document.documentElement.style.setProperty('--app-brightness', String(val))
  }

  /** 每 10 分钟检查一次（应对跨时段） */
  let autoTimer: ReturnType<typeof setInterval> | null = null
  function startAutoCheck() {
    if (autoTimer) clearInterval(autoTimer)
    autoTimer = setInterval(() => {
      if (autoBrightness.value) applyBrightness()
    }, 10 * 60 * 1000)
  }

  function stopAutoCheck() {
    if (autoTimer) { clearInterval(autoTimer); autoTimer = null }
  }

  function setAutoBrightness(v: boolean) {
    autoBrightness.value = v
    localStorage.setItem(AUTO_KEY, String(v))
    applyBrightness()
    if (v) startAutoCheck()
    else stopAutoCheck()
  }

  function setManualBrightness(v: number) {
    manualBrightness.value = v
    localStorage.setItem(STORAGE_KEY, String(v))
    if (!autoBrightness.value) applyBrightness()
  }

  async function loadRemote() {
    if (loaded.value) return
    try {
      const cfg = await get('/system/config')
      if (cfg?.ui?.particles) particles.value = cfg.ui.particles
      if (cfg?.ui?.tilt3d !== undefined) tilt3d.value = !!cfg.ui.tilt3d
      if (cfg?.tts?.auto_speak !== undefined) autoSpeak.value = !!cfg.tts.auto_speak
      if (cfg?.ui?.sound_fx !== undefined) { soundFx.value = !!cfg.ui.sound_fx; sound.setEnabled(soundFx.value) }
      if (cfg?.ui?.sound_volume !== undefined) { soundVolume.value = Number(cfg.ui.sound_volume); sound.setVolume(soundVolume.value) }
      loaded.value = true
    } catch { /* 未登录时静默 */ }
    // 应用亮度
    applyBrightness()
    startAutoCheck()
  }

  function setParticles(v: ParticleDensity) {
    particles.value = v
    localStorage.setItem('ui.particles', v)
    put('/system/config', { path: 'ui.particles', value: v }).catch(() => {})
  }

  function setTilt3d(v: boolean) {
    tilt3d.value = v
    localStorage.setItem('ui.tilt3d', String(v))
    put('/system/config', { path: 'ui.tilt3d', value: v }).catch(() => {})
  }

  async function setAutoSpeak(v: boolean) {
    autoSpeak.value = v
    await put('/media/tts/config', { auto_speak: v })
  }

  function setSoundFx(v: boolean) {
    soundFx.value = v
    sound.setEnabled(v)
    put('/system/config', { path: 'ui.sound_fx', value: v }).catch(() => {})
  }

  function setSoundVolume(v: number) {
    soundVolume.value = v
    sound.setVolume(v)
    put('/system/config', { path: 'ui.sound_volume', value: v }).catch(() => {})
  }

  return {
    particles, tilt3d, autoSpeak, loaded,
    soundFx, soundVolume,
    brightness, autoBrightness, manualBrightness,
    loadRemote, setParticles, setTilt3d, setAutoSpeak,
    setSoundFx, setSoundVolume,
    setAutoBrightness, setManualBrightness, applyBrightness,
    stopAutoCheck,
  }
})