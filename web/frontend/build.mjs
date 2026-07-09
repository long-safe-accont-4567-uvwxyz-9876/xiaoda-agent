/**
 * 前端构建脚本：vite build 前后自动备份/恢复用户自定义壁纸。
 *
 * 问题：vite build 的 emptyOutDir: true 会清空 dist/ 目录，
 *       导致 dist/assets/wallpapers/ 中用户上传的壁纸被删除。
 *
 * 方案：build 前将 wallpapers/ 备份到 /tmp，build 后恢复。
 *       只恢复 build 后不存在的文件（避免覆盖 vite 生成的默认壁纸）。
 */
import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync, statSync } from 'fs'
import { join } from 'path'
import { execSync } from 'child_process'

const __dirname = new URL('.', import.meta.url).pathname
const distWallpapers = join(__dirname, '..', 'dist', 'assets', 'wallpapers')
const backupDir = '/tmp/_wallpaper_backup'

// 备份
if (existsSync(distWallpapers)) {
  mkdirSync(backupDir, { recursive: true })
  let count = 0
  for (const f of readdirSync(distWallpapers)) {
    const fp = join(distWallpapers, f)
    if (statSync(fp).isFile()) {
      writeFileSync(join(backupDir, f), readFileSync(fp))
      count++
    }
  }
  console.log(`[build] backed up ${count} wallpapers`)
}

// vite build
try {
  execSync('npx vite build', { stdio: 'inherit', cwd: __dirname })
} catch (e) {
  console.error('[build] vite build failed:', e.message)
  process.exit(1)
}

// 恢复（只恢复 build 后不存在的文件）
if (existsSync(backupDir)) {
  mkdirSync(distWallpapers, { recursive: true })
  let restored = 0
  for (const f of readdirSync(backupDir)) {
    const target = join(distWallpapers, f)
    if (!existsSync(target)) {
      writeFileSync(target, readFileSync(join(backupDir, f)))
      restored++
    }
  }
  if (restored > 0) {
    console.log(`[build] restored ${restored} user wallpapers`)
  }
}
