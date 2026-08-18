import { useEffect, useMemo, useState } from "react";
import type { Identity, OutboundResult } from "../types";
import { buildOutboundBody, refreshQuery } from "../utils/identity";
import { JsonEditorModal } from "./JsonEditorModal";

/**
 * 代发请求面板：以学习机的身份主动调接口，不必在设备上把界面点一遍。
 *
 * 身份取自已抓到的设备流量（见 utils/identity），所以要先让设备发过一次该 host 的
 * 请求。请求由代理进程发出并绕回代理自身，因此它和设备流量一样会被记录、也一样会被
 * Mock 规则命中。
 */
export function OutboundPanel({
  identities,
  results,
  running,
  onSend,
}: {
  identities: Identity[];
  results: OutboundResult[];
  running: boolean;
  onSend: (payload: {
    id: string;
    url: string;
    method: string;
    headers: Record<string, string>;
    body_plain: string | null;
    encrypt: boolean;
  }) => void;
}) {
  const [host, setHost] = useState("");
  const [url, setUrl] = useState("");
  const [method, setMethod] = useState("POST");
  const [data, setData] = useState("{}");
  const [editorOpen, setEditorOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const identity = useMemo(
    () => identities.find((i) => i.host === host) ?? null,
    [identities, host],
  );

  // 默认选中最新抓到的那个 host，并预填它的来源 URL。
  useEffect(() => {
    if (host || identities.length === 0) return;
    setHost(identities[0].host);
    setUrl(identities[0].source_url);
  }, [identities, host]);

  const send = () => {
    if (!identity) {
      setError("还没抓到该 host 的身份，先在设备上触发一次该 host 的请求");
      return;
    }
    let payloadData: unknown = undefined;
    if (data.trim()) {
      try {
        payloadData = JSON.parse(data);
      } catch (e) {
        setError(`业务内容不是合法 JSON：${e instanceof Error ? e.message : e}`);
        return;
      }
    }
    setError(null);
    onSend({
      id: crypto.randomUUID(),
      url: refreshQuery(url.trim()),
      method,
      headers: identity.headers,
      body_plain: buildOutboundBody(identity, payloadData, method),
      encrypt: identity.encrypted,
    });
  };

  return (
    <div className="outbound-panel">
      <h2>代发请求</h2>

      {identities.length === 0 ? (
        <div className="bp-empty">
          还没抓到任何身份。先启动代理并在设备上触发一次带 Authorization 的请求。
        </div>
      ) : (
        <>
          <div className="field-group">
            <label>身份来源 Host</label>
            <select
              value={host}
              onChange={(e) => {
                setHost(e.target.value);
                // 换了 host 就把 URL 也换成它的来源，否则会拿着上一个 host 的地址去发。
                const next = identities.find((i) => i.host === e.target.value);
                if (next) setUrl(next.source_url);
              }}
              className="outbound-select"
            >
              {identities.map((i) => (
                <option key={i.host} value={i.host}>
                  {i.host}
                  {i.encrypted ? "（密文体）" : "（明文体）"}
                  {i.envelope ? " · 含 base" : ""}
                </option>
              ))}
            </select>
            {identity && (
              <div className="outbound-identity">
                {Object.keys(identity.headers).length} 个请求头 ·{" "}
                {identity.envelope
                  ? `base 字段：${Object.keys(identity.envelope).join(", ")}`
                  : "无身份信封"}
                <br />
                来源：{identity.source_url}
              </div>
            )}
          </div>

          <div className="field-group">
            <label>请求 URL</label>
            <div className="host-input-row">
              <select
                value={method}
                onChange={(e) => setMethod(e.target.value)}
                className="outbound-method"
              >
                <option value="POST">POST</option>
                <option value="GET">GET</option>
                <option value="PUT">PUT</option>
                <option value="DELETE">DELETE</option>
              </select>
              <input
                type="text"
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                placeholder="https://..."
              />
            </div>
          </div>

          <div className="field-group">
            <label>业务内容（会被放进身份信封的 data 字段）</label>
            <pre className="ic-preview" onClick={() => setEditorOpen(true)} title="点击打开编辑器">
              {data || "（空）"}
            </pre>
          </div>

          {error && <div className="outbound-error">{error}</div>}

          <button
            className="btn btn-primary"
            onClick={send}
            disabled={!running || !url.trim()}
            title={running ? "" : "代理未运行"}
          >
            发送
          </button>
        </>
      )}

      {results.length > 0 && (
        <div className="field-group" style={{ marginTop: 24 }}>
          <label>结果</label>
          {results.map((result) => (
            <div
              key={result.id}
              className={`outbound-result ${result.error ? "failed" : ""}`}
            >
              <div className="outbound-result-head">
                <span className={`method-tag ${result.method}`}>{result.method}</span>
                {result.response_status != null && (
                  <span className={`status-code s${Math.floor(result.response_status / 100)}xx`}>
                    {result.response_status}
                  </span>
                )}
                <span className="ic-url">{result.url}</span>
              </div>
              <pre className="ic-preview">{result.error ?? result.response_plain ?? "（空响应）"}</pre>
            </div>
          ))}
        </div>
      )}

      {editorOpen && (
        <JsonEditorModal
          value={data}
          title="业务内容编辑"
          onConfirm={(value) => {
            setData(value);
            setEditorOpen(false);
          }}
          onCancel={() => setEditorOpen(false)}
        />
      )}
    </div>
  );
}
