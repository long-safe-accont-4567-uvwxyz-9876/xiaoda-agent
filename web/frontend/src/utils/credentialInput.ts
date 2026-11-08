/**
 * 凭证类文本（登录密码 / 找回问答）的前端规则。
 *
 * 与后端 security/credential_text.py 保持同一组常量与语义：不做字符集白名单
 * （允许中文、英文、数字与常见符号），长度按 Unicode 字符数计算——用
 * Array.from 取码位数量，而不是按 UTF-16 单元（str.length）或字节数。
 * 修改本文件必须同步后端同名常量。
 */
export const MIN_PASSWORD_LEN = 8
export const MIN_ANSWER_LEN = 6
export const MAX_QUESTION_LEN = 200

export function charLength(value: string): number {
  return Array.from(value || '').length
}

export function normalizeCredential(value: string): string {
  return (value || '').trim()
}

export function isPasswordValid(value: string): boolean {
  return charLength(normalizeCredential(value)) >= MIN_PASSWORD_LEN
}

export function isAnswerValid(value: string): boolean {
  return charLength(normalizeCredential(value)) >= MIN_ANSWER_LEN
}

export function isQuestionValid(value: string): boolean {
  const question = normalizeCredential(value)
  const len = charLength(question)
  return len >= 1 && len <= MAX_QUESTION_LEN
}
