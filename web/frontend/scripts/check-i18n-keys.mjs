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
  process.exit(0)
}

if (missingInEn.length) {
  console.error(`缺 en 翻译 ${missingInEn.length} 条:`)
  for (const k of missingInEn) console.error(`  - ${k}`)
}
if (missingInZh.length) {
  console.error(`缺 zh 翻译 ${missingInZh.length} 条:`)
  for (const k of missingInZh) console.error(`  - ${k}`)
}
process.exit(1)
