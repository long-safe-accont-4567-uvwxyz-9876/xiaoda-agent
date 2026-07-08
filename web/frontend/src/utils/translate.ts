import { pinyin } from 'pinyin-pro'

/**
 * 将中文名称转换为拼音（IP 安全，无硬编码游戏角色名）
 * @param zhName 中文名称
 * @returns 拼音名称（首字母大写）
 */
export function translateToEn(zhName: string): string {
  if (!zhName) return ''
  
  // 使用 pinyin-pro 转换为拼音
  const result = pinyin(zhName, { toneType: 'none', type: 'array' })
  
  // 每个拼音首字母大写，拼接成名字
  return result.map(word => 
    word.charAt(0).toUpperCase() + word.slice(1)
  ).join(' ')
}
