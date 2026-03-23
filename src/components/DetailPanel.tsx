import { useState, useMemo } from "react";
import { JsonView, defaultStyles } from "react-json-view-lite";
import type { RequestRecord } from "../types";
import { formatSize, formatDuration } from "../utils/format";

type DetailTab = "overview" | "request" | "response" | "req-headers" | "resp-headers";

const DETAIL_TABS: { key: DetailTab; label: string }[] = [
  { key: "overview", label: "概览" },
  { key: "request", label: "请求体" },
  { key: "response", label: "响应体" },
  { key: "req-headers", label: "请求头" },
  { key: "resp-headers", label: "响应头" },
];

export { type DetailTab };

export function DetailPanel({
  record,
  tab,
  onTabChange,
}: {
  record: RequestRecord;
  tab: DetailTab;
  onTabChange: (t: DetailTab) => void;
}) {
  return (
    <div className="detail-panel">
      <div className="detail-tabs">
        {DETAIL_TABS.map((t) => (
          <button
            key={t.key}
            className={`detail-tab ${tab === t.key ? "active" : ""}`}
            onClick={() => onTabChange(t.key)}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div className="detail-body">
        {tab === "overview" && <OverviewTab record={record} />}
        {tab === "request" && <JsonBody text={record.request_plain} />}
        {tab === "response" && <JsonBody text={record.response_plain} />}
        {tab === "req-headers" && <HeadersTable headers={record.request_headers} />}
        {tab === "resp-headers" && <HeadersTable headers={record.response_headers} />}
      </div>
    </div>
  );
}

function OverviewTab({ record }: { record: RequestRecord }) {
  const rows: [string, string][] = [
    ["URL", record.url],
    ["Method", record.method],
    [
      "Status",
      record.status === "pending"
        ? "Pending…"
        : record.status === "error"
          ? "Error"
          : record.response_status != null
            ? String(record.response_status)
            : "—",
    ],
    ["Host", record.host],
    ["Path", record.path],
    ["Request Content-Type", record.request_content_type ?? "—"],
    ["Response Content-Type", record.response_content_type ?? "—"],
    ["Request Size", formatSize(record.request_size)],
    ["Response Size", formatSize(record.response_size)],
    ["Duration", formatDuration(record.duration)],
    ["Start Time", new Date(record.start_time).toLocaleTimeString()],
  ];
  return (
    <div className="overview-grid">
      {rows.map(([k, v]) => (
        <div key={k} className="overview-row">
          <span className="overview-key">{k}</span>
          <span className="overview-val">{v}</span>
        </div>
      ))}
    </div>
  );
}

function HeadersTable({
  headers,
}: {
  headers: Record<string, string> | null | undefined;
}) {
  if (!headers || Object.keys(headers).length === 0) {
    return <pre style={{ color: "var(--text-2)" }}>（空）</pre>;
  }
  return (
    <div className="detail-headers">
      {Object.entries(headers).map(([k, v]) => (
        <div key={k} className="hdr-row">
          <span className="hdr-key">{k}</span>
          <span className="hdr-val">{v}</span>
        </div>
      ))}
    </div>
  );
}

function JsonBody({ text }: { text: string | null | undefined }) {
  const [copied, setCopied] = useState(false);

  // Parse once; memoised so large bodies don't get re-parsed on every render.
  const parsed = useMemo(() => {
    if (!text) return null;
    try {
      return JSON.parse(text);
    } catch {
      return null;
    }
  }, [text]);

  if (!text) return <pre style={{ color: "var(--text-2)" }}>（空）</pre>;

  const displayText = parsed != null ? JSON.stringify(parsed, null, 2) : text;
  const jsonViewStyle = useMemo(() => ({ ...defaultStyles, container: "json-tree-root" }), []);

  const handleCopy = () => {
    navigator.clipboard
      .writeText(displayText)
      .then(() => {
        setCopied(true);
        setTimeout(() => setCopied(false), 1500);
      })
      .catch(() => {
        // Clipboard access can be denied by the platform/browser.
      });
  };

  return (
    <div className="json-viewer-container">
      <button className={`btn-copy ${copied ? "copied" : ""}`} onClick={handleCopy}>
        {copied ? "✓ 已复制" : "复制"}
      </button>
      {parsed != null ? (
        <JsonView data={parsed} style={jsonViewStyle} />
      ) : (
        <pre>{text}</pre>
      )}
    </div>
  );
}
