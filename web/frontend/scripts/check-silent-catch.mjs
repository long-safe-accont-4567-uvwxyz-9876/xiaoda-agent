#!/usr/bin/env node
// 动作级空 catch 棘轮：catch 体里既没有语句、也没有 `静默兜底：` 理由注释时报错。
// 目的是避免“改了却默默吞掉错误”的代码再被当成已完成修复。
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'

const root = new URL('../src', import.meta.url).pathname
const files = []
function walk(dir) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    const st = statSync(p)
    if (st.isDirectory()) walk(p)
    else if (/\.(ts|vue|tsx)$/.test(name)) files.push(p)
  }
}
walk(root)

const violations = []
const catchRe = /catch\s*(?:\([^)]*\))?\s*\{/g
for (const file of files) {
  const src = readFileSync(file, 'utf8')
  catchRe.lastIndex = 0
  let m
  while ((m = catchRe.exec(src))) {
    let i = m.index + m[0].length
    let depth = 1
    let j = i
    while (j < src.length && depth > 0) {
      if (src[j] === '{') depth += 1
      else if (src[j] === '}') depth -= 1
      j += 1
    }
    const body = src.slice(i, j - 1)
    const stripped = body.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/[^\n]*/g, '')
    if (stripped.trim() === '' && !body.includes('静默兜底')) {
      const line = src.slice(0, m.index).split('\n').length
      violations.push(`${relative(root, file)}:${line}`)
    }
  }
}

if (violations.length) {
  console.error(`[silent-catch] ${violations.length} 处空 catch 缺少兜底说明：`)
  for (const v of violations) console.error('  ' + v)
  process.exit(1)
}
console.log(`[silent-catch] OK 扫描 ${files.length} 个文件，无缺少理由说明的空 catch`)
