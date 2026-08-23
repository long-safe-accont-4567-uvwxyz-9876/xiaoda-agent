// i18n 双字典 key 一致性校验：zh.ts 与 en.ts 的叶子路径集合必须完全一致。
// 背景（2026-08-22 技术债）：zh/en 各 ~1380 行手工同步，无校验导致缺 key
// 只能在运行时看到裸 key 字符串。本脚本用 esbuild 转译后动态导入对比。
// 用法：npm run check:i18n （缺 key 时退出码 1，逐条列出）
import { build } from 'esbuild'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'

const ROOT = resolve(import.meta.dirname, '..')

async function loadDict(rel) {
  const outDir = mkdtempSync(join(tmpdir(), 'i18n-check-'))
  const outfile = join(outDir, 'dict.mjs')
  try {
    await build({
      entryPoints: [join(ROOT, rel)],
      outfile,
      bundle: true,
      format: 'esm',
      platform: 'node',
      logLevel: 'silent',
    })
    const mod = await import(pathToFileURL(outfile).href)
    return mod.default
  } finally {
    rmSync(outDir, { recursive: true, force: true })
  }
}

function flattenLeaves(obj, prefix = '', acc = []) {
  for (const [k, v] of Object.entries(obj ?? {})) {
    const path = prefix ? `${prefix}.${k}` : k
    if (v && typeof v === 'object' && !Array.isArray(v)) flattenLeaves(v, path, acc)
    else acc.push(path)
  }
  return acc
}

const zh = flattenLeaves(await loadDict('src/i18n/zh.ts'))
const en = flattenLeaves(await loadDict('src/i18n/en.ts'))
const zhSet = new Set(zh)
const enSet = new Set(en)
const missingInEn = zh.filter((k) => !enSet.has(k))
const missingInZh = en.filter((k) => !zhSet.has(k))

if (missingInEn.length === 0 && missingInZh.length === 0) {
  console.log(`i18n keys OK (zh=${zh.length}, en=${en.length}, 完全一致)`)
} else {
  if (missingInEn.length) {
    console.error(`缺 en 翻译 ${missingInEn.length} 条:`)
    for (const k of missingInEn) console.error(`  - ${k}`)
  }
  if (missingInZh.length) {
    console.error(`缺 zh 翻译 ${missingInZh.length} 条:`)
    for (const k of missingInZh) console.error(`  - ${k}`)
  }
  process.exit(1)
}

// ── 硬编码中文棘轮检查 ──────────────────────────────────────────────
// 背景（2026-08-23 技术债复审）：字典 parity 只保证 zh/en 同步，不拦
// "代码里直接写中文不走 t()"——英文 locale 下整页漏中文（RetrievalView/
// LocalDeployView 曾是典型）。本段统计 .vue 模板与字符串字面量中的 CJK
// （剔除注释），与下方 BASELINE 冻结值对比：新文件出现 CJK、或存量文件
// 计数增长 → 退出码 1。清偿一个文件就把它从 BASELINE 删掉（计数归零同样过）。
import { readdirSync, readFileSync, statSync } from 'node:fs'

// g 标志必需：无 g 的 String.match() 只返回首个匹配（长度≤1），
// 棘轮曾因此形同虚设——所有文件计数恒为 0/1，基线全写 1 永远绿灯。
const CJK = /[\u4e00-\u9fff]/g
function* walkVue(dir) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) yield* walkVue(p)
    else if (name.endsWith('.vue')) yield p
  }
}
/** 剥掉 HTML 注释、JS 行/块注释后，剩余文本里的 CJK 即"疑似用户可见硬编码" */
function stripComments(src) {
  return src
    .replace(/<!--[\s\S]*?-->/g, '')
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/^\s*\/\/.*$/gm, '')
}
const counts = new Map()
for (const file of walkVue(join(ROOT, 'src', 'views'))) {
  const rel = file.slice(ROOT.length + 1).replaceAll('\\', '/')
  const n = (stripComments(readFileSync(file, 'utf8')).match(CJK) || []).length
  if (n > 0) counts.set(rel, n)
}

// 存量债务基线（2026-08-23 二次冻结：修复 CJK 正则缺 g 标志后按实测重建，
// 此前非全局正则计数恒 ≤1、基线全写 1，棘轮形同虚设）。
// 只减不增：清偿后请删除对应行；新增文件出现 CJK 即失败。
const BASELINE = {
  'src/views/AgentsView.vue': 92,
  'src/views/ChatView.vue': 106,
  'src/views/HealthView.vue': 24,
  'src/views/InsightView.vue': 79,
  'src/views/MailView.vue': 86,
  'src/views/MediaView.vue': 21,
  'src/views/ModelsView.vue': 223,
  'src/views/PluginsView.vue': 27,
  'src/views/ScheduleView.vue': 17,
  'src/views/SettingsView.vue': 57,
  'src/views/SetupWizardView.vue': 23,
  'src/views/ToolsView.vue': 2,
  'src/views/WorkflowView.vue': 208,
}
let bad = 0
for (const [rel, n] of [...counts].sort()) {
  const base = BASELINE[rel]
  if (base === undefined) {
    console.error(`i18n 棘轮：新文件出现硬编码 CJK ${rel} (${n} 处)——请走 t() + zh/en 字典`)
    bad++
  } else if (n > base) {
    console.error(`i18n 棘轮：${rel} 硬编码 CJK 从基线 ${base} 增至 ${n}`)
    bad++
  }
}
if (bad === 0) {
  console.log(`i18n hardcoded-CJK ratchet OK (${counts.size} 个存量文件在基线内)`)
} else {
  process.exit(1)
}
