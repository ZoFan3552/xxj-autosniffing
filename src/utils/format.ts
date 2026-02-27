export function formatSize(bytes: number | null | undefined): string {
  if (bytes == null) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function formatDuration(ms: number | null | undefined): string {
  if (ms == null) return "—";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(2)} s`;
}

export function shortContentType(ct: string | null | undefined): string {
  if (!ct) return "—";
  const map: Record<string, string> = {
    "application/json": "JSON",
    "text/html": "HTML",
    "text/plain": "Text",
    "text/css": "CSS",
    "text/xml": "XML",
    "application/xml": "XML",
    "application/javascript": "JS",
    "text/javascript": "JS",
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/gif": "GIF",
    "image/webp": "WebP",
    "image/svg+xml": "SVG",
    "application/octet-stream": "Binary",
    "application/x-protobuf": "Protobuf",
    "application/grpc": "gRPC",
  };
  return map[ct] ?? ct.split("/").pop()?.split(";")[0] ?? ct;
}
