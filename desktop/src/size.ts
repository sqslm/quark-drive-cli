export function formatSize(bytes:number|null) {
  if(bytes === null || !Number.isFinite(bytes)) return '—';
  if(bytes < 1024) return `${(bytes / 1024).toFixed(3)} KB`;
  if(bytes < 1048576) return `${(bytes / 1024).toFixed(1)} KB`;
  if(bytes < 1073741824) return `${(bytes / 1048576).toFixed(1)} MB`;
  return `${(bytes / 1073741824).toFixed(2)} GB`;
}
