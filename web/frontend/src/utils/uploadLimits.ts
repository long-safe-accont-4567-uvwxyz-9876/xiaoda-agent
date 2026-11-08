/**
 * 上传体积/类型前端预检（审查 M2）。
 *
 * 原先图片与文档上传没有任何前端体积校验，超限只能等后端 read_upload_limited
 * 抛 400；而 usePromptUploads 的 catch 又把任何异常都收敛成一句"上传失败"，
 * 服务端的具体原因（文件过大 / 类型不支持 / 413）被丢弃。
 *
 * 上限与后端 web/routers/chat.py 保持一致：
 *   MAX_IMAGE_SIZE = 10MB、MAX_DOC_SIZE = 20MB（音频 20MB 走语音录制路径）。
 */

export const IMAGE_MAX_BYTES = 10 * 1024 * 1024
export const DOC_MAX_BYTES = 20 * 1024 * 1024

export type UploadKind = 'image' | 'doc'

export interface UploadCheck {
  ok: boolean
  /** TOO_LARGE 由前端拦下；服务端错误仍以服务端文案为准 */
  code?: 'TOO_LARGE'
  limitBytes?: number
}

export function limitFor(kind: UploadKind): number {
  return kind === 'image' ? IMAGE_MAX_BYTES : DOC_MAX_BYTES
}

/** 人类可读体积：10MB / 1.5MB / 512KB */
export function humanSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '0B'
  if (bytes >= 1024 * 1024) {
    const mb = bytes / (1024 * 1024)
    return `${Number.isInteger(mb) ? mb : mb.toFixed(1)}MB`
  }
  if (bytes >= 1024) return `${Math.round(bytes / 1024)}KB`
  return `${Math.round(bytes)}B`
}

export function checkUploadSize(size: number, kind: UploadKind): UploadCheck {
  const limit = limitFor(kind)
  if (!Number.isFinite(size) || size < 0) return { ok: false, code: 'TOO_LARGE', limitBytes: limit }
  if (size > limit) return { ok: false, code: 'TOO_LARGE', limitBytes: limit }
  return { ok: true, limitBytes: limit }
}
