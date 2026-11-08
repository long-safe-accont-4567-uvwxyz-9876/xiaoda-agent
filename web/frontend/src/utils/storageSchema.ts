/**
 * localStorage schema 版本与类型收敛（审查 D1）。
 *
 * 现状：token / expires_at / lang / xiaoda_profile_done / xiaoda_disclaimer_agreed /
 * ui.* 若干 / ws.authorizedDirs 都是裸值，没有任何版本号。旧版本写入的对象结构
 * 在新版本读取时没有迁移或校验分支——例如某个 ui.* 哪天从布尔改成对象，老用户
 * 会拿到旧类型而不报错（`!== 'false'` 之类的判断会静默走偏）。
 *
 * 这里在启动时做一次"版本校验 + 类型收敛"：版本不一致就按当前 schema 逐个校验
 * 已知键，类型不符的直接清掉（读取方回落到默认值），最后写回新版本号。
 * 新增键必须先登记到 STORAGE_SPEC，否则不参与校验（棘轮式约束）。
 */

export const STORAGE_SCHEMA_KEY = 'storage.schema'
export const STORAGE_SCHEMA_VERSION = 2

export type StorageKind = 'string' | 'number' | 'bool' | 'json'

export interface StorageSpec {
  key: string
  kind: StorageKind
}

/** 当前已知的全部持久化键与期望类型 */
export const STORAGE_SPEC: readonly StorageSpec[] = [
  { key: 'token', kind: 'string' },
  { key: 'expires_at', kind: 'string' },
  { key: 'lang', kind: 'string' },
  { key: 'xiaoda_profile_done', kind: 'string' },
  { key: 'xiaoda_disclaimer_agreed', kind: 'string' },
  { key: 'ui.particles', kind: 'string' },
  { key: 'ui.tilt3d', kind: 'bool' },
  { key: 'ui.dendroCursor', kind: 'bool' },
  { key: 'ui.dendroCursorTrail', kind: 'bool' },
  { key: 'ui.brightness', kind: 'number' },
  { key: 'ui.autoBrightness', kind: 'bool' },
  { key: 'ui.soundFx', kind: 'bool' },
  { key: 'ui.soundVolume', kind: 'number' },
  { key: 'ws.authorizedDirs', kind: 'json' },
]

/** UI store 里允许出现的粒子密度取值；越界值同样按"不兼容"处理 */
const PARTICLES_ALLOWED = new Set(['off', 'low', 'medium', 'high'])

export interface StorageLike {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
  removeItem(key: string): void
}

/** 单值是否符合期望类型（空字符串视为"未设置"，不算不兼容） */
export function isValidStoredValue(raw: string | null, kind: StorageKind): boolean {
  if (raw === null || raw === '') return true
  switch (kind) {
    case 'string':
      // '[object Object]' / 'undefined' 是"用错写入方式"的典型痕迹
      return raw !== '[object Object]' && raw !== 'undefined' && raw !== 'null'
    case 'bool':
      return raw === 'true' || raw === 'false'
    case 'number':
      return raw.trim() !== '' && Number.isFinite(Number(raw))
    case 'json':
      try {
        const parsed = JSON.parse(raw)
        return parsed !== null && typeof parsed === 'object'
      } catch {
        return false
      }
    default:
      return true
  }
}

export interface StorageMigrationResult {
  version: number
  migrated: boolean
  dropped: string[]
}

/**
 * 校验并收敛持久化数据。幂等：版本已经是当前值时不触碰任何键。
 * 不兼容的键直接删除，读取方据此回落到代码里的默认值。
 */
export function ensureStorageSchema(
  storage: StorageLike,
  version: number = STORAGE_SCHEMA_VERSION,
): StorageMigrationResult {
  const storedVersion = Number(storage.getItem(STORAGE_SCHEMA_KEY) || 0)
  if (storedVersion === version) return { version, migrated: false, dropped: [] }

  const dropped: string[] = []
  for (const spec of STORAGE_SPEC) {
    const raw = storage.getItem(spec.key)
    if (raw === null) continue
    let ok = isValidStoredValue(raw, spec.kind)
    // 枚举型键额外校验取值范围
    if (ok && spec.key === 'ui.particles' && raw !== '' && !PARTICLES_ALLOWED.has(raw)) ok = false
    if (!ok) {
      storage.removeItem(spec.key)
      dropped.push(spec.key)
    }
  }
  storage.setItem(STORAGE_SCHEMA_KEY, String(version))
  return { version, migrated: true, dropped }
}

/** 布尔读取：只有显式 'true' 为真，其余（含脏值）走默认值 */
export function readBool(storage: StorageLike, key: string, fallback: boolean): boolean {
  const raw = storage.getItem(key)
  if (raw === 'true') return true
  if (raw === 'false') return false
  return fallback
}

/** 数字读取：NaN/越界一律回落默认值 */
export function readNumber(storage: StorageLike, key: string, fallback: number): number {
  const raw = storage.getItem(key)
  if (raw === null || raw.trim() === '') return fallback
  const n = Number(raw)
  return Number.isFinite(n) ? n : fallback
}

/** JSON 读取：解析失败或类型不符时回落默认值（不抛错） */
export function readJson<T>(storage: StorageLike, key: string, fallback: T): T {
  const raw = storage.getItem(key)
  if (!raw) return fallback
  try {
    const parsed = JSON.parse(raw)
    if (Array.isArray(fallback)) return (Array.isArray(parsed) ? parsed : fallback) as T
    if (parsed === null || typeof parsed !== typeof fallback) return fallback
    return parsed as T
  } catch {
    return fallback
  }
}

/** 字符串读取（'undefined'/'null' 这类脏值按未设置处理） */
export function readString(storage: StorageLike, key: string, fallback = ''): string {
  const raw = storage.getItem(key)
  if (raw === null || raw === 'undefined' || raw === 'null') return fallback
  return raw
}
