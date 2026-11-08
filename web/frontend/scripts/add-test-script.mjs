// 一次性：把 test:unit 脚本登记进 package.json（幂等）
import { readFileSync, writeFileSync } from 'node:fs'
const p = JSON.parse(readFileSync('package.json', 'utf8'))
p.scripts['test:unit'] = 'node scripts/run-unit-tests.mjs'
writeFileSync('package.json', JSON.stringify(p, null, 2) + '\n')
console.log('test:unit ->', p.scripts['test:unit'])
