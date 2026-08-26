export interface ClipboardImageHit {
  found: boolean
  file: File | null
}

export function firstImageItemFile(items: DataTransferItemList | null): ClipboardImageHit {
  if (!items) return { found: false, file: null }
  for (const item of items) {
    if (item.type.startsWith('image/')) return { found: true, file: item.getAsFile() }
  }
  return { found: false, file: null }
}
