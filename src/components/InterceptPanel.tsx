import { useState, useMemo } from "react";
import type { InterceptRequest } from "../types";
import { JsonEditorModal } from "./JsonEditorModal";

export function InterceptPanel({
  intercepts,
  bodies,
  onBodyChange,
  onRespond,
}: {
  intercepts: InterceptRequest[];
  bodies: Record<string, string>;
  onBodyChange: (id: string, val: string) => void;
  onRespond: (id: string, action: string, currentBody?: string) => void;
}) {
  return (
    <div className="intercept-panel">
      {intercepts.length === 0 ? (
        <div className="empty-state" style={{ minHeight: 200 }}>
          <div className="empty-icon">🛡️</div>
          <p>暂无待处理的拦截请求</p>
          <p style={{ fontSize: 10 }}>匹配规则的请求将在此处等待审核</p>
        </div>
      ) : (
        intercepts.map((req) => (
          <InterceptCard
            key={req.flow_id}
            req={req}
            body={bodies[req.flow_id]}
            onBodyChange={(val) => onBodyChange(req.flow_id, val)}
            onRespond={(action, currentBody) => onRespond(req.flow_id, action, currentBody)}
          />
        ))
      )}
    </div>
  );
}

function InterceptCard({
  req,
  body,
  onBodyChange,
  onRespond,
}: {
  req: InterceptRequest;
  body: string | undefined;
  onBodyChange: (val: string) => void;
  onRespond: (action: string, currentBody?: string) => void;
}) {
  const [editorOpen, setEditorOpen] = useState(false);

  const initialBody = useMemo(() => {
    if (req.body_plain) {
      try {
        return JSON.stringify(JSON.parse(req.body_plain), null, 2);
      } catch {
        return req.body_plain;
      }
    }
    return "";
  }, [req.body_plain]);

  const currentText = body ?? initialBody;
  const passBody = body !== undefined ? body : (req.body_plain ?? undefined);
  const phaseLabel = req.phase === "response" ? "响应体" : "请求体";
  const phaseBadge = req.phase === "response" ? "响应" : "请求";

  // 预览文本：截取前几行
  const previewText = useMemo(() => {
    const lines = currentText.split("\n");
    if (lines.length <= 6) return currentText;
    return lines.slice(0, 6).join("\n") + "\n...";
  }, [currentText]);

  const handleEditorConfirm = (newValue: string) => {
    onBodyChange(newValue);
    setEditorOpen(false);
  };

  return (
    <div className="intercept-card">
      <div className="ic-header">
        <span className={`method-tag ${req.method}`}>{req.method}</span>
        <span className={`ic-phase-badge ${req.phase}`}>{phaseBadge}</span>
        {req.phase === "response" && req.status_code != null && (
          <span className="ic-status">{req.status_code}</span>
        )}
        <span className="ic-url">{req.url}</span>
      </div>

      <div className="ic-preview-section">
        <div className="ic-editor-toolbar">
          <span className="ic-editor-label">{phaseLabel}</span>
          <button className="btn btn-sm btn-primary" onClick={() => setEditorOpen(true)}>
            ✎ 编辑
          </button>
        </div>
        <pre
          className="ic-preview"
          onClick={() => setEditorOpen(true)}
          title="点击打开编辑器"
        >
          {previewText || "（空）"}
        </pre>
      </div>

      <div className="ic-actions">
        <button className="btn btn-danger btn-sm" onClick={() => onRespond("abort")}>
          ✕ 丢弃
        </button>
        <button className="btn btn-success btn-sm" onClick={() => onRespond("pass", passBody)}>
          ✓ 放行
        </button>
      </div>

      {editorOpen && (
        <JsonEditorModal
          value={currentText}
          title={`${phaseLabel}编辑 — ${req.method} ${req.url}`}
          onConfirm={handleEditorConfirm}
          onCancel={() => setEditorOpen(false)}
        />
      )}
    </div>
  );
}
