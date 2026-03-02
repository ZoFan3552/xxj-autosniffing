export interface Config {
  encrypt_url: string;
  decrypt_url: string;
  proxy_port: number;
  /** Host allowlist for capturing. Suffix matching (e.g. "xunfeixxj.com" matches "api.xunfeixxj.com"). Empty = capture all. */
  capture_hosts: string[];
  /** Charles-style breakpoint rules */
  breakpoints: BreakpointRule[];
}

export interface EnvironmentCheckItem {
  key: string;
  label: string;
  ok: boolean;
  detail: string;
  hint?: string;
}

export interface EnvironmentCheckResult {
  all_ok: boolean;
  items: EnvironmentCheckItem[];
}

export interface BreakpointRule {
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
