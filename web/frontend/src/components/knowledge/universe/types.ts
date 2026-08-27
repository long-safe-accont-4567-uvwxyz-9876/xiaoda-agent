/**
 * 纳西妲宇宙 —— 共享类型定义（从 UniverseGraph.vue 拆出）
 */
import type * as THREE from 'three'
import type { NodeObject } from '3d-force-graph'

export interface GraphNode extends NodeObject {
  name: string
  kind?: string
  val?: number
  /** 世界树枝干锚点（引擎指派；锚点弹簧力据此安放节点） */
  __anchor?: import('three').Vector3
}

export interface GraphLink {
  source: string | GraphNode
  target: string | GraphNode
  relation?: string
  /** 关系主键（删/改连接凭此定位；graph 接口 2026-08-27 起返回） */
  id?: string
}

export interface StarLayer {
  points: THREE.Points
  speed: number
}

export interface Ripple {
  mesh: THREE.Mesh
  startTime: number
  duration: number
}

export interface OrbitLikeControls {
  autoRotate: boolean
  autoRotateSpeed: number
  update?: (delta?: number) => void
  target?: THREE.Vector3
  mouseButtons?: { LEFT: number; MIDDLE: number; RIGHT: number }
}

/** 性能档位：high=Bloom 开/双粒子，medium=灯关/单粒子，low=无粒子/6 段低模节点 */
export type QualityTier = 'high' | 'medium' | 'low'
