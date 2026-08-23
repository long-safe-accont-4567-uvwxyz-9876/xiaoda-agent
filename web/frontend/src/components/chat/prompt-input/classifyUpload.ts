export type UploadClassification = { kind: 'image' } | { kind: 'doc'; ext: string } | null

export function classifyUpload(file: File): UploadClassification {
  const isImage = file.type.startsWith('image/')
  if (isImage) return { kind: 'image' }
  const docExts = ['.pdf', '.docx', '.doc', '.pptx', '.ppt', '.xlsx', '.xls', '.txt', '.md']
  const ext = '.' + (file.name.split('.').pop() || '').toLowerCase()
  if (docExts.includes(ext)) return { kind: 'doc', ext }
  return null
}
