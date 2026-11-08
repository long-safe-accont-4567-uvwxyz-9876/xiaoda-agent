#!/usr/bin/env node
/**
 * 前端单元测试运行器（零新增依赖）。
 *
 * 仓库没有 vitest/jsdom 依赖，也不想为一个测试拉进整套浏览器环境：
 * 这里用已有的 esbuild 把 tests/*.spec.ts 打成单文件 ESM，
 * 再交给 node:test 跑。被测模块都是纯逻辑（不碰真实 DOM），
 * 需要的全局（localStorage / document / AbortController）在加载前打桩。
 *
 * 用法：node scripts/run-unit-tests.mjs [spec 文件...]
 */
import { build } from 'esbuild'
import { mkdtemp, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { readdir } from 'node:fs/promises'

const specDir = new URL('../tests/', import.meta.url)
const args = process.argv.slice(2)
const specs = args.length
  ? args
  : (await readdir(specDir)).filter(f => f.endsWith('.spec.ts')).map(f => new URL(f, specDir).pathname)

if (!specs.length) {
  console.error('no spec files found under tests/')
  process.exit(1)
}

// ── 全局桩：被测模块在 import 期就会读这些 ──
const store = new Map()
globalThis.localStorage = {
  getItem: k => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => { store.set(k, String(v)) },
  removeItem: k => { store.delete(k) },
  clear: () => store.clear(),
}
globalThis.document = {
  hidden: false,
  addEventListener: () => {},
  removeEventListener: () => {},
  createElement: () => ({ innerHTML: '', content: { firstChild: null } }),
  createTextNode: () => ({}),
  querySelector: () => null,
}
globalThis.window = globalThis

// 产物必须落在项目内：external 的 'vue' 走 Node 解析，从 /tmp 出发找不到 node_modules
const dir = await mkdtemp(join(process.cwd(), 'node_modules', '.fe-unit-'))
for (const spec of specs) {
  const outfile = join(dir, spec.replace(/[\\/]/g, '_').replace(/\.ts$/, '.mjs'))
  await build({
    entryPoints: [spec],
    outfile,
    bundle: true,
    format: 'esm',
    platform: 'node',
    target: 'node18',
    external: ['node:*', 'vue'],
    logLevel: 'warning',
  })
  await writeFile(outfile + '.marker', '')
  await import(pathToFileURL(outfile).href)
}
