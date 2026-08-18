export interface Config {
  encrypt_url: string;
  decrypt_url: string;
  /** Raw secret for in-process crypto. Non-empty takes priority over the two URLs above. */
  crypto_secret: string;
  /** Whether crypto_secret is base64-encoded rather than plain text. */
  crypto_secret_b64: boolean;
  proxy_port: number;
  /** Host allowlist for capturing. Suffix matching (e.g. "xunfeixxj.com" matches "api.xunfeixxj.com"). Empty = capture all. */
  capture_hosts: string[];
  /** Charles-style breakpoint rules */
  breakpoints: BreakpointRule[];
  /** Mock rules, checked before breakpoints */
  mock_rules: MockRule[];
}

export interface MockRule {
  /** Stable key, also used for React rendering */
  id: string;
  name: string;
  /** URL regex pattern to match */
  url_pattern: string;
  /** Empty means any method */
  method: string;
  /** "respond" answers without contacting the server; "patch" merges into the real response */
  mode: "respond" | "patch";
  /** Response status for "respond" mode */
  status: number;
  /** Plain response body for "respond" mode, or the JSON fragment for "patch" mode */
  body: string;
  /** Hit cap; 0 means unlimited */
  times: number;
  delay_ms: number;
  /** Whether the body is encrypted before being sent back */
  encrypt: boolean;
  enabled: boolean;
}

export interface BreakpointRule {
  /** UI-only stable key for React rendering */
  id?: string;
  /** URL regex pattern to match */
  url_pattern: string;
  /** Break on request phase */
  break_request: boolean;
  /** Break on response phase */
  break_response: boolean;
  /** Whether this rule is active */
  enabled: boolean;
}

export interface RequestRecord {
  flow_id: string;
  /** Sequence number assigned by the proxy */
  seq: number;
  method: string;
  url: string;
  /** Parsed host from URL */
  host: string;
  /** Parsed path from URL */
  path: string;
  /** "device" for real device traffic, "outbound" for requests this app sent itself */
  origin?: "device" | "outbound";
  /** Set when a mock rule handled this request */
  mock?: { rule: string; mode: string } | null;
  /** Request Content-Type */
  request_content_type: string | null;
  request_headers: Record<string, string>;
  request_plain: string | null;
  /** Request body size in bytes */
  request_size: number;
  response_status: number | null;
  /** Response Content-Type */
  response_content_type: string | null;
  response_headers: Record<string, string> | null;
  response_plain: string | null;
  /** Response body size in bytes */
  response_size: number | null;
  /** Timestamp when request started (epoch ms) */
  start_time: number;
  /** Duration in milliseconds (null if response not yet received) */
  duration: number | null;
  /** "pending" | "complete" | "error" | "aborted" */
  status: "pending" | "complete" | "error" | "aborted";
}

export interface InterceptRequest {
  flow_id: string;
  phase: "request" | "response";
  method: string;
  url: string;
  body_plain: string | null;
  status_code?: number;
}

export interface WsFrame {
  /** Connection number assigned by the proxy, unique within a proxy run */
  conn: number;
  /** Frame number within its connection */
  seq: number;
  url: string;
  host: string;
  /** Milliseconds since this connection's handshake */
  t_ms: number;
  /** "up" is client to server, "down" is server to client */
  dir: "up" | "down";
  type: "text" | "binary";
  size: number;
  /** True when this frame was injected by a replay rather than seen on the wire */
  injected: boolean;
  /** Text payload as-is; binary payload as base64 */
  payload: string;
}

export interface WsConn {
  conn: number;
  url: string;
  host: string;
  path: string;
  state: "open" | "closed";
  frames: number;
  /** True when this connection is being answered by a replay instead of the server */
  replaying: boolean;
  /** Handshake time (epoch ms) */
  start_time: number;
}

/** A credential and identity snapshot for one host, derived from recorded device traffic. */
export interface Identity {
  host: string;
  headers: Record<string, string>;
  /** The identity part of the request body; the business gateway calls it "base". */
  envelope: Record<string, unknown> | null;
  /** Whether this host's bodies travel encrypted */
  encrypted: boolean;
  source_url: string;
  captured_at: number;
}

export interface OutboundResult {
  id: string;
  url: string;
  method: string;
  encrypted?: boolean;
  request_plain?: string | null;
  response_status?: number;
  response_plain?: string | null;
  error?: string | null;
}
