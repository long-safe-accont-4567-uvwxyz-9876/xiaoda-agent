/**
 * 草元素音效引擎 —— 全程序化 WebAudio 合成，零音频文件依赖。
 *
 * 音色设计（A 大调五声音阶，清脆、柔和、自然系）：
 *  - send    叶拂：短促上扬的风噪扫频 + 一声轻叶颤音（发消息）
 *  - receive 风铃：E6/B6 双音晶亮拨弦 + 空气感延迟尾巴（收到回复）
 *  - click   露珠：一滴高处落下的水珠，音高微降，极轻（按钮/导航点击）
 *  - notify  晨铃：A5→C#6→E6 三音琶音，三角波柔和晨钟（问候推送）
 *  - toggle  嫩芽：一声短促上挑的芽音（开关切换）
 *
 * 浏览器自动播放策略：AudioContext 惰性创建，首次用户手势时 resume 解锁。
 */

export type SoundKind = 'send' | 'receive' | 'click' | 'notify' | 'toggle'

const ENABLED_KEY = 'ui.soundFx'
const VOLUME_KEY = 'ui.soundVolume'

class SoundEngine {
  private ctx: AudioContext | null = null
  private master: GainNode | null = null
  private wet: DelayNode | null = null
  private enabled: boolean = localStorage.getItem(ENABLED_KEY) !== 'false'
  private volume: number = parseFloat(localStorage.getItem(VOLUME_KEY) || '0.5')
  private unlocked = false
  private lastPlay: Record<string, number> = {}

  isEnabled(): boolean { return this.enabled }
  getVolume(): number { return this.volume }

  setEnabled(v: boolean) {
    this.enabled = v
    localStorage.setItem(ENABLED_KEY, String(v))
    if (v) this.ensureCtx()
  }

  setVolume(v: number) {
    this.volume = Math.min(1, Math.max(0, v))
    localStorage.setItem(VOLUME_KEY, String(this.volume))
    if (this.master && this.ctx) {
      this.master.gain.setTargetAtTime(this.volume * 0.6, this.ctx.currentTime, 0.03)
    }
  }

  /** 惰性创建音频图：master → destination，wet(延迟混响) → master */
  private ensureCtx(): AudioContext | null {
    if (this.ctx) {
      if (this.ctx.state === 'suspended') this.ctx.resume().catch(() => {})
      return this.ctx
    }
    try {
      const AC = window.AudioContext || (window as any).webkitAudioContext
      if (!AC) return null
      this.ctx = new AC()

      this.master = this.ctx.createGain()
      this.master.gain.value = this.volume * 0.6
      this.master.connect(this.ctx.destination)

      // 简易空气感：feedback delay 模拟林间回响
      const delay = this.ctx.createDelay(0.5)
      delay.delayTime.value = 0.14
      const fb = this.ctx.createGain()
      fb.gain.value = 0.22
      const wetGain = this.ctx.createGain()
      wetGain.gain.value = 0.18
      delay.connect(fb)
      fb.connect(delay)
      delay.connect(wetGain)
      wetGain.connect(this.master)
      this.wet = delay

      if (this.ctx.state === 'suspended') this.ctx.resume().catch(() => {})
      return this.ctx
    } catch {
      return null
    }
  }

  /** 首次用户手势时调用，解锁音频 */
  unlock() {
    if (this.unlocked) return
    this.unlocked = true
    this.ensureCtx()
  }

  /** 晶亮拨弦：基频 + 2 倍频微光，快攻慢衰 */
  private pluck(freq: number, at: number, dur: number, peak: number, dest?: AudioNode) {
    const ctx = this.ctx!
    const t0 = ctx.currentTime + at
    for (const [mult, gain] of [[1, 1], [2, 0.28]] as const) {
      const osc = ctx.createOscillator()
      osc.type = 'sine'
      osc.frequency.value = freq * mult
      const g = ctx.createGain()
      g.gain.setValueAtTime(0, t0)
      g.gain.linearRampToValueAtTime(peak * gain, t0 + 0.008)
      g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur)
      osc.connect(g)
      g.connect(dest || this.master!)
      if (this.wet && !dest) g.connect(this.wet)
      osc.start(t0)
      osc.stop(t0 + dur + 0.05)
    }
  }

  /** 柔和钟音：三角波 + 慢一点的起音 */
  private bell(freq: number, at: number, dur: number, peak: number) {
    const ctx = this.ctx!
    const t0 = ctx.currentTime + at
    const osc = ctx.createOscillator()
    osc.type = 'triangle'
    osc.frequency.value = freq
    const g = ctx.createGain()
    g.gain.setValueAtTime(0, t0)
    g.gain.linearRampToValueAtTime(peak, t0 + 0.02)
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur)
    osc.connect(g)
    g.connect(this.master!)
    if (this.wet) g.connect(this.wet)
    osc.start(t0)
    osc.stop(t0 + dur + 0.05)
  }

  /** 风噪扫频：带通滤波的白噪声，叶片拂动感 */
  private whoosh(at: number, dur: number, from: number, to: number, peak: number) {
    const ctx = this.ctx!
    const t0 = ctx.currentTime + at
    const len = Math.ceil(ctx.sampleRate * dur)
    const buf = ctx.createBuffer(1, len, ctx.sampleRate)
    const data = buf.getChannelData(0)
    for (let i = 0; i < len; i++) data[i] = Math.random() * 2 - 1
    const src = ctx.createBufferSource()
    src.buffer = buf
    const bp = ctx.createBiquadFilter()
    bp.type = 'bandpass'
    bp.Q.value = 1.8
    bp.frequency.setValueAtTime(from, t0)
    bp.frequency.exponentialRampToValueAtTime(to, t0 + dur)
    const g = ctx.createGain()
    g.gain.setValueAtTime(0, t0)
    g.gain.linearRampToValueAtTime(peak, t0 + dur * 0.3)
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur)
    src.connect(bp)
    bp.connect(g)
    g.connect(this.master!)
    src.start(t0)
  }

  play(kind: SoundKind) {
    if (!this.enabled) return
    // 防抖：同类音效 60ms 内不重复触发
    const now = performance.now()
    if (now - (this.lastPlay[kind] || 0) < 60) return
    this.lastPlay[kind] = now

    const ctx = this.ensureCtx()
    if (!ctx || ctx.state !== 'running') return

    switch (kind) {
      case 'send':
        // 叶拂：上扬风噪 + 清脆小颤音
        this.whoosh(0, 0.16, 900, 3200, 0.10)
        this.pluck(1568, 0.05, 0.28, 0.10) // G6 轻点
        break
      case 'receive':
        // 风铃：E6 → B6 双音错落，晶亮收尾
        this.pluck(1318.5, 0, 0.55, 0.16)
        this.pluck(1975.5, 0.09, 0.7, 0.11)
        break
      case 'click': {
        // 露珠：一滴水，音高 880→520 滑落
        const t0 = ctx.currentTime
        const osc = ctx.createOscillator()
        osc.type = 'sine'
        osc.frequency.setValueAtTime(880, t0)
        osc.frequency.exponentialRampToValueAtTime(520, t0 + 0.07)
        const g = ctx.createGain()
        g.gain.setValueAtTime(0, t0)
        g.gain.linearRampToValueAtTime(0.09, t0 + 0.006)
        g.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.11)
        osc.connect(g)
        g.connect(this.master!)
        osc.start(t0)
        osc.stop(t0 + 0.14)
        break
      }
      case 'notify':
        // 晨铃：A5 → C#6 → E6 琶音，三角波柔和
        this.bell(880, 0, 0.8, 0.10)
        this.bell(1108.7, 0.12, 0.8, 0.09)
        this.bell(1318.5, 0.24, 1.0, 0.10)
        break
      case 'toggle':
        // 嫩芽：短促上挑
        this.pluck(1046.5, 0, 0.18, 0.10)
        break
    }
  }
}

export const sound = new SoundEngine()
