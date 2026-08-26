/**
 * 纳西妲宇宙 —— 点击涟漪（从 UniverseGraph.vue 拆出）
 *
 * 在节点位置生成线框球体，向外扩散并淡出；过期 mesh 的 geometry/material
 * 释放时序与拆分前一致：先移出场景（若引擎实例还在），再无条件 dispose。
 */
import * as THREE from 'three'
import type { Ripple } from './types'

export interface RippleManager {
  /** 调用方需保证引擎实例存在（scene 有效） */
  spawn(scene: THREE.Scene, x: number, y: number, z: number, color: string): void
  /** 每帧推进动画；scene 为 null 表示实例已销毁（跳过 remove，释放照常执行） */
  update(scene: THREE.Scene | null): void
  /** 卸载清理：释放全部存活涟漪 */
  disposeAll(scene: THREE.Scene | null): void
}

const RIPPLE_DURATION_MS = 800
const RIPPLE_MAX_SCALE = 25

export function createRippleManager(): RippleManager {
  let ripples: Ripple[] = []

  function spawn(scene: THREE.Scene, x: number, y: number, z: number, color: string): void {
    const geo = new THREE.SphereGeometry(1, 16, 12)
    const mat = new THREE.MeshBasicMaterial({
      color: new THREE.Color(color),
      transparent: true,
      opacity: 0.6,
      wireframe: true,
      depthWrite: false,
    })
    const mesh = new THREE.Mesh(geo, mat)
    mesh.position.set(x, y, z)
    scene.add(mesh)
    ripples.push({ mesh, startTime: performance.now(), duration: RIPPLE_DURATION_MS })
  }

  function update(scene: THREE.Scene | null): void {
    if (!ripples.length) return
    const now = performance.now()
    ripples = ripples.filter(r => {
      const progress = (now - r.startTime) / r.duration
      if (progress >= 1) {
        scene?.remove(r.mesh)
        r.mesh.geometry.dispose()
        ;(r.mesh.material as THREE.Material).dispose()
        return false
      }
      // ease-out 扩散
      const scale = 1 + (1 - (1 - progress) * (1 - progress)) * RIPPLE_MAX_SCALE
      r.mesh.scale.setScalar(scale)
      ;(r.mesh.material as THREE.MeshBasicMaterial).opacity = 0.6 * (1 - progress)
      return true
    })
  }

  function disposeAll(scene: THREE.Scene | null): void {
    for (const r of ripples) {
      scene?.remove(r.mesh)
      r.mesh.geometry.dispose()
      ;(r.mesh.material as THREE.Material).dispose()
    }
    ripples = []
  }

  return { spawn, update, disposeAll }
}
