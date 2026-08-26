/**
 * 纳西妲宇宙 —— 世界树场景层（程序化古树 + 枝尖锚点）
 *
 * 恢复最初那棵确定性递归生成的世界树（Fibonacci 式分枝、梢端萤光、
 * 冠层辉光、地面光环、上升光尘），并让它承担记忆容器职责：
 *   - 每个 MAX_LEVEL 末梢就是一个"枝尖锚点"——一球一梢；
 *   - 引擎把相关记忆安放到父节点枝尖附近的空梢上，关系近的记忆长在同一根大枝；
 *   - 超出梢容量时由黄金角螺旋兜底在树冠外围生成漂浮光球位。
 *
 * 风摆顶点着色器与 windOffset() 公式严格一致 —— 球摇枝也摇。
 */
import * as THREE from 'three'
import { mergeGeometries } from 'three/examples/jsm/utils/BufferGeometryUtils.js'

// ── 树形常量（与最初版本一致；MAX_LEVEL 加一层以提供更多枝尖）──
const TRUNK_LEN = 130          // 主干长度
const TRUNK_RADIUS = 7         // 干基半径
const MAX_LEVEL = 5            // 分枝层数（0=主干 … 5=末梢细枝 → 4×3⁴=324 个枝尖）
const CHILDREN_ROOT = 4        // 主干分出的大枝数
const CHILDREN = 3             // 其余层级分枝数
const LEN_FALLOFF = 0.7        // 子枝长度衰减
const RADIUS_FALLOFF = 0.62    // 子枝半径衰减
const SEGMENTS_PER_BRANCH = 3  // 每段枝的圆柱分段数

export interface WorldTree {
  /** 全部枝尖锚点（世界坐标，含群组偏移；已按 近干/居中优先 排序；自动生长的新梢会追加在尾部） */
  anchors: THREE.Vector3[]
  /** 建议初始相机距离 */
  viewDistance: number
  /** 树群组（引擎用于拖拽时的树冠倾斜弹性） */
  group: THREE.Group
  /** 群组垂直偏移：local = scene.y - originY（风摆公式入参用） */
  readonly originY: number
  /** 树内时钟（秒），与风摆着色器共用 */
  readonly time: number
  /**
   * 风摆位移公式（与枝干顶点着色器严格一致）。
   * 引擎用它让记忆球跟随树枝一起摇 —— 球长在枝尖，风来同摆。
   */
  windOffset(localY: number, t: number, out: THREE.Vector3): THREE.Vector3
  /** anchors 耗尽时的兜底：树冠范围内第 i 个黄金角螺旋点 */
  canopyAnchor(i: number): THREE.Vector3
  /**
   * 世界树自动生长：从 fromWorld（通常是父球所在的枝尖）延伸一根真实的新梢，
   * 新梢末端作为锚点追加进 anchors 并返回 —— 记忆涨满时树随之长大。
   * 几何合并延迟到下一帧统一执行（一批展开只重建一次动态层）。
   */
  extendBranch(fromWorld: THREE.Vector3): THREE.Vector3
  /** 生长脉冲：新记忆落梢时树皮发光涌动一下 */
  pulseGrowth(): void
  /** 每帧推进：梢端风摆 + 光尘上升 + 冠层呼吸 + 动态枝干合批 + 脉冲衰减（dt 秒） */
  update(dt: number): void
  dispose(scene: THREE.Scene): void
}

/** mulberry32：确定性随机，保证每次进入全屏树形一致 */
function mulberry32(seed: number): () => number {
  let a = seed >>> 0
  return () => {
    a |= 0; a = (a + 0x6D2B79F5) | 0
    let t = Math.imul(a ^ (a >>> 15), 1 | a)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

/** 径向渐变贴图（冠层辉光 / 地面光晕共用生成器） */
function makeGlowTexture(inner: string, outer: string): THREE.Texture {
  const size = 128
  const canvas = document.createElement('canvas')
  canvas.width = canvas.height = size
  const g = canvas.getContext('2d')!
  const grad = g.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2)
  grad.addColorStop(0, inner)
  grad.addColorStop(1, outer)
  g.fillStyle = grad
  g.fillRect(0, 0, size, size)
  const tex = new THREE.CanvasTexture(canvas)
  tex.colorSpace = THREE.SRGBColorSpace
  return tex
}

interface BranchSpec {
  from: THREE.Vector3
  dir: THREE.Vector3
  len: number
  radius: number
  level: number
}

export function buildWorldTree(scene: THREE.Scene): WorldTree {
  const rand = mulberry32(20260823)
  const group = new THREE.Group()
  const disposables: Array<{ dispose(): void }> = []

  // ── 递归生成分枝：圆柱段几何收集 + 末梢（枝尖）记录 ──
  const branchGeos: THREE.BufferGeometry[] = []
  const tipPositions: number[] = []

  const tmpUp = new THREE.Vector3(0, 1, 0)
  const tmpQuat = new THREE.Quaternion()

  function addCylinder(a: THREE.Vector3, b: THREE.Vector3, rBottom: number, rTop: number, into: THREE.BufferGeometry[] = branchGeos): void {
    const dir = b.clone().sub(a)
    const len = dir.length()
    if (len < 1e-3) return
    const geo = new THREE.CylinderGeometry(rTop, rBottom, len, 5, 1, true)
    tmpQuat.setFromUnitVectors(tmpUp, dir.normalize())
    geo.applyQuaternion(tmpQuat)
    geo.translate((a.x + b.x) / 2, (a.y + b.y) / 2, (a.z + b.z) / 2)
    into.push(geo)
  }

  function growBranch(spec: BranchSpec): void {
    // 弯曲路径：逐段加入抖动与轻微向上趋势
    let p = spec.from.clone()
    let d = spec.dir.clone().normalize()
    const pts: THREE.Vector3[] = [p.clone()]
    for (let s = 0; s < SEGMENTS_PER_BRANCH; s++) {
      d.x += (rand() - 0.5) * 0.22
      d.z += (rand() - 0.5) * 0.22
      if (spec.level > 0) d.y += 0.05 // 侧枝微微向光上翘
      d.normalize()
      p = p.clone().addScaledVector(d, spec.len / SEGMENTS_PER_BRANCH)
      pts.push(p.clone())
    }
    // 圆柱段：沿路径渐细
    for (let s = 0; s < pts.length - 1; s++) {
      const f0 = 1 - (s / (pts.length - 1)) * 0.28
      const f1 = 1 - ((s + 1) / (pts.length - 1)) * 0.28
      addCylinder(pts[s], pts[s + 1], spec.radius * f0, spec.radius * f1)
    }

    if (spec.level >= MAX_LEVEL) {
      tipPositions.push(p.x, p.y, p.z) // 枝尖 = 记忆球锚点候选
      return
    }
    // 子枝：绕父方向均匀布向 + 随机倾角
    const kids = spec.level === 0 ? CHILDREN_ROOT : CHILDREN
    const baseAz = rand() * Math.PI * 2
    // 构建与 d 垂直的基底
    const n1 = new THREE.Vector3().crossVectors(d, Math.abs(d.y) < 0.9 ? tmpUp : new THREE.Vector3(1, 0, 0)).normalize()
    const n2 = new THREE.Vector3().crossVectors(d, n1).normalize()
    for (let k = 0; k < kids; k++) {
      const az = baseAz + (k * Math.PI * 2) / kids + (rand() - 0.5) * 0.6
      const tilt = 0.55 + rand() * 0.35
      const nd = d.clone()
        .multiplyScalar(Math.cos(tilt))
        .addScaledVector(n1, Math.cos(az) * Math.sin(tilt))
        .addScaledVector(n2, Math.sin(az) * Math.sin(tilt))
        .normalize()
      growBranch({
        from: p,
        dir: nd,
        len: spec.len * LEN_FALLOFF * (0.88 + rand() * 0.24),
        radius: spec.radius * RADIUS_FALLOFF,
        level: spec.level + 1,
      })
    }
  }

  growBranch({
    from: new THREE.Vector3(0, 0, 0),
    dir: new THREE.Vector3((rand() - 0.5) * 0.06, 1, (rand() - 0.5) * 0.06),
    len: TRUNK_LEN,
    radius: TRUNK_RADIUS,
    level: 0,
  })

  // ── 整体高度：估算各级总长，把树垂直居中到 y≈0 ──
  let totalH = 0
  for (let l = 0; l <= MAX_LEVEL; l++) totalH += TRUNK_LEN * Math.pow(LEN_FALLOFF, l)
  const groupY = -totalH * 0.46
  group.position.y = groupY

  // ── 合并全部枝干为单一几何（1 draw call）──
  const mergedGeo = mergeGeometries(branchGeos, false)!
  branchGeos.forEach(g => g.dispose())

  // ── 风摆顶点着色器：越靠枝头摆幅越大，整棵树有生命的呼吸感 ──
  // 公式在 windOffset() 中严格复刻（梢端光点 / 记忆球共用），保证球不离枝
  const WIND_X_AMP = 2.8
  const WIND_Z_AMP = 2.4
  const windUniform = { value: 0 }
  let flash = 0
  const barkMat = new THREE.MeshLambertMaterial({
    color: '#39744e',
    emissive: '#1c4530',
    emissiveIntensity: 0.55,
  })
  barkMat.onBeforeCompile = (shader) => {
    shader.uniforms.uTime = windUniform
    shader.vertexShader = 'uniform float uTime;\n' + shader.vertexShader.replace(
      '#include <begin_vertex>',
      [
        '#include <begin_vertex>',
        `float hFactor = smoothstep(0.0, ${totalH.toFixed(1)}, transformed.y);`,
        `transformed.x += sin(uTime * 0.55 + transformed.y * 0.008) * hFactor * ${WIND_X_AMP.toFixed(1)};`,
        `transformed.z += cos(uTime * 0.42 + transformed.y * 0.01) * hFactor * ${WIND_Z_AMP.toFixed(1)};`,
      ].join('\n'),
    )
  }
  const trunkMesh = new THREE.Mesh(mergedGeo, barkMat)
  disposables.push(mergedGeo, barkMat)
  group.add(trunkMesh)

  // ── 梢端光点（每帧按风摆公式同步位移，与枝尖严丝合缝）──
  const tipBase = Float32Array.from(tipPositions)
  const tipGeo = new THREE.BufferGeometry()
  tipGeo.setAttribute('position', new THREE.Float32BufferAttribute(tipPositions, 3))
  const tipMat = new THREE.PointsMaterial({
    color: '#b8f28a',
    size: 4.2,
    transparent: true,
    opacity: 0.9,
    blending: THREE.AdditiveBlending,
    depthWrite: false,
    sizeAttenuation: true,
  })
  const tipPoints = new THREE.Points(tipGeo, tipMat)
  group.add(tipPoints)
  disposables.push(tipGeo, tipMat)

  // ── 动态生长层：梢容量耗尽后自动延伸的新枝（合批合并，同享风摆材质）──
  const dynGeos: THREE.BufferGeometry[] = []
  let dynDirty = false
  let extCounter = 0
  const dynMesh = new THREE.Mesh(new THREE.BufferGeometry(), barkMat)
  dynMesh.frustumCulled = false
  group.add(dynMesh)

  // ── 冠层辉光 Sprite ──
  const glowTex = makeGlowTexture('rgba(143,229,96,0.55)', 'rgba(143,229,96,0)')
  const glowMat = new THREE.SpriteMaterial({
    map: glowTex,
    transparent: true,
    opacity: 0.12,
    blending: THREE.AdditiveBlending,
    depthWrite: false,
  })
  const canopy = new THREE.Sprite(glowMat)
  const crownY = totalH - TRUNK_LEN * Math.pow(LEN_FALLOFF, MAX_LEVEL) * 0.8
  canopy.position.set(0, crownY, 0)
  canopy.scale.setScalar(totalH * 0.95)
  group.add(canopy)
  disposables.push(glowTex, glowMat)

  // ── 地面光晕 + 双光环 ──
  const groundTex = makeGlowTexture('rgba(143,229,96,0.4)', 'rgba(143,229,96,0)')
  const groundMat = new THREE.MeshBasicMaterial({
    map: groundTex,
    transparent: true,
    opacity: 0.22,
    blending: THREE.AdditiveBlending,
    depthWrite: false,
    side: THREE.DoubleSide,
  })
  const ground = new THREE.Mesh(new THREE.CircleGeometry(totalH * 0.72, 48), groundMat)
  ground.rotation.x = -Math.PI / 2
  ground.position.y = 0.2
  group.add(ground)
  disposables.push(groundTex, groundMat, ground.geometry)

  for (const [r, op] of [[totalH * 0.26, 0.26], [totalH * 0.5, 0.14]] as const) {
    const ringGeo = new THREE.RingGeometry(r, r + 1.4, 64)
    const ringMat = new THREE.MeshBasicMaterial({
      color: '#8fe560',
      transparent: true,
      opacity: op,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
      side: THREE.DoubleSide,
    })
    const ring = new THREE.Mesh(ringGeo, ringMat)
    ring.rotation.x = -Math.PI / 2
    ring.position.y = 0.6
    group.add(ring)
    disposables.push(ringGeo, ringMat)
  }

  // ── 上升光尘（生命之息）──
  const MOTES = 220
  const motePos = new Float32Array(MOTES * 3)
  const moteSpeed = new Float32Array(MOTES)
  const moteAngle = new Float32Array(MOTES)
  const moteRadius = new Float32Array(MOTES)
  const moteRot = new Float32Array(MOTES)
  for (let i = 0; i < MOTES; i++) {
    moteAngle[i] = rand() * Math.PI * 2
    moteRadius[i] = 12 + rand() * 105
    motePos[i * 3] = Math.cos(moteAngle[i]) * moteRadius[i]
    motePos[i * 3 + 1] = rand() * totalH
    motePos[i * 3 + 2] = Math.sin(moteAngle[i]) * moteRadius[i]
    moteSpeed[i] = 10 + rand() * 22
    moteRot[i] = (rand() - 0.5) * 1.4
  }
  const moteGeo = new THREE.BufferGeometry()
  moteGeo.setAttribute('position', new THREE.BufferAttribute(motePos, 3))
  const moteMat = new THREE.PointsMaterial({
    color: '#e8d5a3',
    size: 1.8,
    transparent: true,
    opacity: 0.55,
    blending: THREE.AdditiveBlending,
    depthWrite: false,
    sizeAttenuation: true,
  })
  group.add(new THREE.Points(moteGeo, moteMat))
  disposables.push(moteGeo, moteMat)

  scene.add(group)

  // ── 枝尖锚点：世界坐标 + 按「近轴、近冠心」排序（根记忆球取首个）──
  const anchors = (() => {
    const pts: THREE.Vector3[] = []
    for (let i = 0; i < tipPositions.length; i += 3) {
      pts.push(new THREE.Vector3(
        tipPositions[i],
        tipPositions[i + 1] + groupY,
        tipPositions[i + 2],
      ))
    }
    const score = (v: THREE.Vector3) => Math.hypot(v.x, v.z) * 1.6 + Math.abs(v.y - (groupY + crownY))
    return pts.sort((a, b) => score(a) - score(b))
  })()

  const maxRadial = Math.max(...anchors.map(a => Math.hypot(a.x, a.z)), 60)
  const canopyAnchor = (i: number): THREE.Vector3 => {
    // 黄金比例低差异序列铺在树冠椭球面附近（梢容量耗尽时的兜底），
    // 不取模回绕 —— 溢出再多也不会重叠到同一点
    const goldenRatio = 0.618033988749895
    const t = (i * goldenRatio) % 1
    const az = i * Math.PI * (3 - Math.sqrt(5))
    const ph = Math.acos(1 - 2 * t)
    const rx = maxRadial * 1.15
    const ry = totalH * 0.16
    return new THREE.Vector3(
      rx * Math.sin(ph) * Math.cos(az),
      groupY + crownY + ry * Math.cos(ph),
      rx * Math.sin(ph) * Math.sin(az),
    )
  }

  let time = 0

  // ── 动态新枝：从父球枝尖向外延伸（离轴向外 + 上翘 + 确定性抖动）──
  function extendBranch(fromWorld: THREE.Vector3): THREE.Vector3 {
    const local = new THREE.Vector3(fromWorld.x, fromWorld.y - groupY, fromWorld.z)
    const rnd = mulberry32(
      (extCounter++) * 2654435761 ^ Math.round(local.x * 7 + local.y * 13 + local.z * 17),
    )
    const rad = Math.hypot(local.x, local.z)
    const outX = rad > 1 ? local.x / rad : Math.cos(rnd() * Math.PI * 2)
    const outZ = rad > 1 ? local.z / rad : Math.sin(rnd() * Math.PI * 2)
    // 方向 = 离轴向外为主，向上翘为辅，切向抖动防呆板
    const tj = (rnd() - 0.5) * 0.7
    let dx = outX - outZ * tj
    let dz = outZ + outX * tj
    let dy = 0.3 + rnd() * 0.4
    const dl = Math.hypot(dx, dy, dz)
    dx /= dl; dy /= dl; dz /= dl
    const dir = new THREE.Vector3(dx, dy, dz)
    const len = 26 + rnd() * 18
    const mid = local.clone().addScaledVector(dir, len * 0.5)
    const end = local.clone().addScaledVector(dir, len)
    if (end.y < 6) { // 不穿地
      const lift = 6 - end.y
      end.y += lift
      mid.y += lift * 0.6
    }
    addCylinder(local, mid, 1.7, 1.15, dynGeos)
    addCylinder(mid, end, 1.15, 0.7, dynGeos)
    dynDirty = true
    flash = 1
    const world = new THREE.Vector3(end.x, end.y + groupY, end.z)
    anchors.push(world)
    return world
  }

  /** 一帧至多重建一次动态层几何（一批展开 N 根新梢只合并一次） */
  function flushDyn(): void {
    if (!dynDirty) return
    dynDirty = false
    if (!dynGeos.length) return
    const merged = mergeGeometries(dynGeos, false)!
    dynGeos.forEach(g => g.dispose())
    dynGeos.length = 0
    const old = dynMesh.geometry
    dynMesh.geometry = merged
    old.dispose()
  }

  /** 风摆位移 —— 与枝干顶点着色器公式严格一致（局部坐标，y 为树局部高度） */
  const windOffset = (localY: number, t: number, out: THREE.Vector3): THREE.Vector3 => {
    // 与 GLSL smoothstep(0, totalH, y) 一致的 hermite 插值
    const raw = Math.min(1, Math.max(0, localY / totalH))
    const h = raw * raw * (3 - 2 * raw)
    return out.set(
      Math.sin(t * 0.55 + localY * 0.008) * h * WIND_X_AMP,
      0,
      Math.cos(t * 0.42 + localY * 0.01) * h * WIND_Z_AMP,
    )
  }
  const windTmp = new THREE.Vector3()

  return {
    anchors,
    viewDistance: totalH * 1.55,
    group,
    originY: groupY,
    get time() { return time },
    windOffset,
    canopyAnchor,
    extendBranch,
    pulseGrowth(): void { flash = 1 },
    update(dt: number): void {
      time += dt
      flushDyn()
      windUniform.value = time
      // 生长脉冲衰减 → 树皮微光回落
      if (flash > 0.001) {
        flash *= Math.exp(-dt * 2.2)
        barkMat.emissiveIntensity = 0.55 + flash * 0.9
      }
      // 梢端光点同步风摆（数量有限，CPU 逐点复算可忽略）
      const posAttr = tipGeo.attributes.position as THREE.BufferAttribute
      for (let i = 0; i < tipBase.length; i += 3) {
        windOffset(tipBase[i + 1], time, windTmp)
        posAttr.array[i] = tipBase[i] + windTmp.x
        posAttr.array[i + 2] = tipBase[i + 2] + windTmp.z
      }
      posAttr.needsUpdate = true
      // 光尘螺旋上升，越顶回卷
      for (let i = 0; i < MOTES; i++) {
        moteAngle[i] += moteRot[i] * dt
        motePos[i * 3] = Math.cos(moteAngle[i]) * moteRadius[i]
        motePos[i * 3 + 2] = Math.sin(moteAngle[i]) * moteRadius[i]
        motePos[i * 3 + 1] += moteSpeed[i] * dt
        if (motePos[i * 3 + 1] > totalH) motePos[i * 3 + 1] = 0
      }
      moteGeo.attributes.position.needsUpdate = true
      // 冠层呼吸
      glowMat.opacity = 0.11 + 0.04 * Math.sin(time * 0.8)
    },
    dispose(sc: THREE.Scene): void {
      sc.remove(group)
      dynGeos.forEach(g => g.dispose())
      dynGeos.length = 0
      for (const d of disposables) d.dispose()
      dynMesh.geometry.dispose()
    },
  }
}
