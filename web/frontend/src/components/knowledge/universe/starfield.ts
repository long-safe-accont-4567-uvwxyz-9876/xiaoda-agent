/**
 * 纳西妲宇宙 —— 三层星空背景（从 UniverseGraph.vue 拆出）
 *
 * Fibonacci 螺旋分布 + HSL 闪烁（借鉴 Obsidian 粒子星图）。
 * 渲染期只做旋转增量；geometry/material 的释放仅在卸载清理时执行，
 * 不影响任何渲染期视觉行为。
 */
import * as THREE from 'three'
import { COLOR_DENDRO, COLOR_MOON, COLOR_WISDOM } from './theme'
import type { StarLayer } from './types'

interface StarLayerConfig {
  count: number
  rMin: number
  rMax: number
  size: number
  color: string
  opacity: number
  speed: number
}

// 星层数量略减以兼顾性能；Fibonacci 分布比随机分布更均匀优雅
const STAR_LAYERS_CONFIG: StarLayerConfig[] = [
  { count: 600, rMin: 800, rMax: 1000, size: 1, color: COLOR_DENDRO, opacity: 0.3, speed: 0.00015 },
  { count: 300, rMin: 400, rMax: 600, size: 1.5, color: COLOR_WISDOM, opacity: 0.5, speed: 0.00028 },
  { count: 150, rMin: 200, rMax: 300, size: 2, color: COLOR_MOON, opacity: 0.7, speed: 0.00045 },
]

export function createStarLayers(scene: THREE.Scene): StarLayer[] {
  return STAR_LAYERS_CONFIG.map(cfg => {
    const positions = new Float32Array(cfg.count * 3)
    // 顶点颜色：HSL 随机亮度，模拟星星闪烁
    const colors = new Float32Array(cfg.count * 3)
    const base = new THREE.Color(cfg.color)
    const hsl = { h: 0, s: 0, l: 0 }
    base.getHSL(hsl)
    for (let i = 0; i < cfg.count; i++) {
      // Fibonacci 球面分布：phi 均匀铺纬度，theta 螺旋铺经度 → 星云光带效果
      const phi = Math.acos(-1 + (2 * i) / cfg.count)
      const theta = Math.sqrt(cfg.count * Math.PI) * phi
      const r = cfg.rMin + Math.random() * (cfg.rMax - cfg.rMin)
      positions[i * 3] = r * Math.cos(theta) * Math.sin(phi)
      positions[i * 3 + 1] = r * Math.cos(phi)
      positions[i * 3 + 2] = r * Math.sin(theta) * Math.sin(phi)
      // HSL 随机亮度（0.6~1.0），让星星有明暗差异，模拟闪烁
      const c = new THREE.Color().setHSL(hsl.h, hsl.s, hsl.l * (0.6 + Math.random() * 0.4))
      colors[i * 3] = c.r
      colors[i * 3 + 1] = c.g
      colors[i * 3 + 2] = c.b
    }
    const geo = new THREE.BufferGeometry()
    geo.setAttribute('position', new THREE.BufferAttribute(positions, 3))
    geo.setAttribute('color', new THREE.BufferAttribute(colors, 3))
    const mat = new THREE.PointsMaterial({
      size: cfg.size,
      vertexColors: true,
      transparent: true,
      opacity: cfg.opacity,
      sizeAttenuation: true,
      depthWrite: false,
    })
    const pts = new THREE.Points(geo, mat)
    scene.add(pts)
    return { points: pts, speed: cfg.speed }
  })
}

/** 每帧自转增量（独立 RAF 由引擎驱动，渲染由引擎每帧执行） */
export function spinStarLayers(layers: StarLayer[]): void {
  for (const layer of layers) {
    layer.points.rotation.y += layer.speed
    layer.points.rotation.x += layer.speed * 0.3
  }
}

/** 卸载清理：逐层移出场景并释放 geometry/material */
export function disposeStarLayers(layers: StarLayer[], scene: THREE.Scene): void {
  for (const layer of layers) {
    scene.remove(layer.points)
    layer.points.geometry.dispose()
    ;(layer.points.material as THREE.Material).dispose()
  }
}
