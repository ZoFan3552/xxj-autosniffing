import type { Identity, RequestRecord } from "../types";

/**
 * Identity snapshots: the credentials and identity fields an outbound request needs
 * in order to pass for the device.
 *
 * They are derived from the traffic already recorded rather than configured: the
 * device is not rooted and the app cannot be `run-as`, so a request that flowed
 * through the proxy is the only place these values are visible.
 *
 * Snapshots are kept per host, not per path, because outbound requests mostly probe
 * endpoints the device has never called — a per-path snapshot would have no history
 * to draw on.
 */

/** Headers recomputed per request or tied to the connection; copying them only breaks things. */
const VOLATILE_HEADERS = new Set([
  "content-length",
  "host",
  "connection",
  "proxy-connection",
  "transfer-encoding",
  "expect",
  "upgrade",
  "keep-alive",
  "te",
  "trailer",
]);

/** Identity envelope fields that must carry a fresh value on every outbound request. */
const TRACE_FIELDS = ["traceId", "monitorTraceId"];
const TIMESTAMP_FIELD = "timestamp";

/** 32 hex characters, the same shape as the device's `base.traceId`. */
export function newTraceId(): string {
  return crypto.randomUUID().replace(/-/g, "");
}

function byteLength(text: string): number {
  return new TextEncoder().encode(text).length;
}

/**
 * Whether this host's request bodies are cipher text.
 *
 * Decided by whether the lengths line up: a plain body travels as-is so the two match,
 * while a cipher body has been through gzip and AES and cannot match.
 */
function bodyEncrypted(rawSize: number | null, plain: string | null): boolean {
  if (!plain || !rawSize) return false;
  return rawSize !== byteLength(plain);
}

/** Pull the identity envelope out of a plain request body, or null if it isn't there. */
function pickEnvelope(requestPlain: string | null): Record<string, unknown> | null {
  if (!requestPlain) return null;
  try {
    const document = JSON.parse(requestPlain);
    const envelope = document?.base;
    return envelope && typeof envelope === "object" && !Array.isArray(envelope)
      ? (envelope as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

function hasAuthorization(headers: Record<string, string>): boolean {
  return Object.keys(headers).some((k) => k.toLowerCase() === "authorization");
}

/**
 * Derive one snapshot per host from the recorded traffic, newest wins.
 *
 * Requests without a credential header do not constitute an identity, and outbound
 * requests are skipped — they were built from a snapshot and cannot produce a new one.
 */
export function deriveIdentities(records: RequestRecord[]): Identity[] {
  const byHost = new Map<string, Identity>();
  // Records arrive newest-first, so walk backwards to let newer ones overwrite.
  for (let i = records.length - 1; i >= 0; i--) {
    const record = records[i];
    if (record.origin === "outbound") continue;
    if (!record.host || !hasAuthorization(record.request_headers)) continue;

    const previous = byHost.get(record.host);
    const headers: Record<string, string> = {};
    for (const [k, v] of Object.entries(record.request_headers)) {
      if (!VOLATILE_HEADERS.has(k.toLowerCase())) headers[k] = v;
    }
    // A bodyless request (a GET, say) cannot tell whether this host encrypts, so it
    // inherits the previous verdict rather than resetting an encrypted host to plain.
    const encrypted = record.request_plain
      ? bodyEncrypted(record.request_size, record.request_plain)
      : Boolean(previous?.encrypted);

    byHost.set(record.host, {
      host: record.host,
      headers,
      envelope: pickEnvelope(record.request_plain) ?? previous?.envelope ?? null,
      encrypted,
      source_url: record.url,
      captured_at: record.start_time,
    });
  }
  return [...byHost.values()].sort((a, b) => b.captured_at - a.captured_at);
}

/**
 * Build the plain request body: the caller supplies the business content, the
 * snapshot supplies the identity envelope with fresh trace fields.
 *
 * Returns null for methods that carry no body — holding an envelope does not mean
 * every endpoint wants one.
 */
export function buildOutboundBody(
  identity: Identity,
  data: unknown,
  method: string,
): string | null {
  if (["GET", "HEAD", "OPTIONS"].includes(method.toUpperCase())) return null;
  if (!identity.envelope) {
    return data === undefined || data === null ? null : JSON.stringify(data);
  }

  const envelope: Record<string, unknown> = { ...identity.envelope };
  for (const field of TRACE_FIELDS) {
    if (field in envelope) envelope[field] = newTraceId();
  }
  if (TIMESTAMP_FIELD in envelope) envelope[TIMESTAMP_FIELD] = String(Date.now());

  const document: Record<string, unknown> = { base: envelope };
  if (data !== undefined && data !== null) document.data = data;
  return JSON.stringify(document);
}

/**
 * Replace the trace parameters already present in the URL with fresh values.
 *
 * Only keys that are already written in the URL are touched — reusing an old traceId
 * would file two calls under one trace on the server, and untangling that is exactly
 * why outbound requests exist.
 */
export function refreshQuery(url: string): string {
  try {
    const parsed = new URL(url);
    if (!parsed.search) return url;
    for (const field of TRACE_FIELDS) {
      if (parsed.searchParams.has(field)) parsed.searchParams.set(field, newTraceId());
    }
    return parsed.toString();
  } catch {
    return url;
  }
}
