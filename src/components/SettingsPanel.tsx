import { useEffect, useState } from "react";
import type { Config, BreakpointRule, MockRule } from "../types";
import { JsonEditorModal } from "./JsonEditorModal";

export function SettingsPanel({
  config,
  onChange,
  onSave,
  dirty,
}: {
  config: Config;
  onChange: (c: Config) => void;
  onSave: () => void;
  dirty: boolean;
}) {
  const update = <K extends keyof Config>(key: K, val: Config[K]) =>
    onChange({ ...config, [key]: val });

  const [newHost, setNewHost] = useState("");

  // Ensure every breakpoint has a stable UI key.
  useEffect(() => {
    if (config.breakpoints.every((bp) => bp.id)) return;
    update(
      "breakpoints",
      config.breakpoints.map((bp) =>
        bp.id ? bp : { ...bp, id: crypto.randomUUID() },
      ),
    );
  }, [config.breakpoints]);

  // Same for mock rules, which a hand-edited config file may be missing.
  useEffect(() => {
    if (config.mock_rules.every((rule) => rule.id)) return;
    update(
      "mock_rules",
      config.mock_rules.map((rule) =>
        rule.id ? rule : { ...rule, id: crypto.randomUUID() },
      ),
    );
  }, [config.mock_rules]);

  const addHost = () => {
    const h = newHost.trim().toLowerCase();
    if (h && !config.capture_hosts.includes(h)) {
      update("capture_hosts", [...config.capture_hosts, h]);
    }
    setNewHost("");
  };

  const removeHost = (host: string) => {
    update(
      "capture_hosts",
      config.capture_hosts.filter((h) => h !== host),
    );
  };

  const addBreakpoint = () => {
    update("breakpoints", [
      ...config.breakpoints,
      {
        id: crypto.randomUUID(),
        url_pattern: "",
        break_request: true,
        break_response: false,
        enabled: true,
      },
    ]);
  };

  const updateBreakpoint = (idx: number, patch: Partial<BreakpointRule>) => {
    update(
      "breakpoints",
      config.breakpoints.map((bp, i) => (i === idx ? { ...bp, ...patch } : bp)),
    );
  };

  const removeBreakpoint = (idx: number) => {
    update(
      "breakpoints",
      config.breakpoints.filter((_, i) => i !== idx),
    );
  };

  const addMockRule = () => {
    update("mock_rules", [
      ...config.mock_rules,
      {
        id: crypto.randomUUID(),
        name: `规则 ${config.mock_rules.length + 1}`,
        url_pattern: "",
        method: "",
        mode: "respond" as const,
        status: 200,
        body: "",
        times: 0,
        delay_ms: 0,
        encrypt: true,
        enabled: true,
      },
    ]);
  };

  const updateMockRule = (idx: number, patch: Partial<MockRule>) => {
    update(
      "mock_rules",
      config.mock_rules.map((r, i) => (i === idx ? { ...r, ...patch } : r)),
    );
  };

  const removeMockRule = (idx: number) => {
    update(
      "mock_rules",
      config.mock_rules.filter((_, i) => i !== idx),
    );
  };

  return (
    <div className="settings-panel">
      <h2>配置</h2>

      <div className="field-group">
        <label>抓包 Host 列表（后缀匹配，留空则抓取全部）</label>
        <div className="host-list">
          {config.capture_hosts.map((h) => (
            <span key={h} className="host-tag">
              {h}
              <button className="host-tag-remove" onClick={() => removeHost(h)}>
                ×
              </button>
            </span>
          ))}
        </div>
        <div className="host-input-row">
          <input
            type="text"
            value={newHost}
            onChange={(e) => setNewHost(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                addHost();
              }
            }}
            placeholder="输入域名后回车添加，如 xunfeixxj.com"
          />
          <button className="btn btn-sm" onClick={addHost} disabled={!newHost.trim()}>
            添加
          </button>
        </div>
      </div>

      <div className="field-group">
        <label>代理端口</label>
        <input
          type="number"
          value={config.proxy_port}
          onChange={(e) => {
            const v = parseInt(e.target.value, 10);
            if (!Number.isNaN(v) && v >= 1 && v <= 65535) {
              update("proxy_port", v);
            }
          }}
        />
      </div>

      <div className="field-group">
        <label>本地密钥（填了就在本机加解密，不再调用下面的接口）</label>
        <input
          type="text"
          value={config.crypto_secret}
          onChange={(e) => update("crypto_secret", e.target.value)}
          placeholder="取自 App 二进制的原始密钥，留空则使用加解密接口"
        />
        <label className="checkbox-row" style={{ marginTop: 8 }}>
          <input
            type="checkbox"
            checked={config.crypto_secret_b64}
            onChange={(e) => update("crypto_secret_b64", e.target.checked)}
          />
          密钥是 Base64 编码（与 App 源码里的存法一致）
        </label>
      </div>

      <div className="field-group">
        <label>加密接口地址（仅在内网使用）</label>
        <input
          type="text"
          value={config.encrypt_url}
          onChange={(e) => update("encrypt_url", e.target.value)}
          placeholder="http://..."
        />
      </div>

      <div className="field-group">
        <label>解密接口地址（仅在内网使用）</label>
        <input
          type="text"
          value={config.decrypt_url}
          onChange={(e) => update("decrypt_url", e.target.value)}
          placeholder="http://..."
        />
      </div>

      <div className="field-group">
        <label>断点规则（指定要 Mock 的接口）</label>
        {config.breakpoints.length === 0 ? (
          <div className="bp-empty">暂无断点规则，点击下方按钮添加</div>
        ) : (
          <div className="bp-table">
            <div className="bp-header">
              <span className="bp-col-enabled">启用</span>
              <span className="bp-col-pattern">URL 匹配（正则）</span>
              <span className="bp-col-check">请求</span>
              <span className="bp-col-check">响应</span>
              <span className="bp-col-action"></span>
            </div>
            {config.breakpoints.map((bp, idx) => (
              <div key={bp.id ?? `${idx}-${bp.url_pattern}`} className={`bp-row ${bp.enabled ? "" : "bp-disabled"}`}>
                <span className="bp-col-enabled">
                  <label className="toggle">
                    <input
                      type="checkbox"
                      checked={bp.enabled}
                      onChange={(e) => updateBreakpoint(idx, { enabled: e.target.checked })}
                    />
                    <span className="slider" />
                  </label>
                </span>
                <span className="bp-col-pattern">
                  <input
                    type="text"
                    value={bp.url_pattern}
                    onChange={(e) => updateBreakpoint(idx, { url_pattern: e.target.value })}
                    placeholder="例如 /api/v1/.*"
                  />
                </span>
                <span className="bp-col-check">
                  <input
                    type="checkbox"
                    checked={bp.break_request}
                    onChange={(e) => updateBreakpoint(idx, { break_request: e.target.checked })}
                  />
                </span>
                <span className="bp-col-check">
                  <input
                    type="checkbox"
                    checked={bp.break_response}
                    onChange={(e) => updateBreakpoint(idx, { break_response: e.target.checked })}
                  />
                </span>
                <span className="bp-col-action">
                  <button
                    className="btn-bp-remove"
                    onClick={() => removeBreakpoint(idx)}
                    title="删除"
                  >
                    ×
                  </button>
                </span>
              </div>
            ))}
          </div>
        )}
        <button className="btn btn-sm" onClick={addBreakpoint} style={{ marginTop: 6 }}>
          + 添加规则
        </button>
      </div>

      <div className="field-group">
        <label>Mock 规则（命中后直接应答或改写响应，优先于断点）</label>
        {config.mock_rules.length === 0 ? (
          <div className="bp-empty">暂无 Mock 规则，点击下方按钮添加</div>
        ) : (
          config.mock_rules.map((rule, idx) => (
            <MockRuleCard
              key={rule.id}
              rule={rule}
              onChange={(patch) => updateMockRule(idx, patch)}
              onRemove={() => removeMockRule(idx)}
            />
          ))
        )}
        <button className="btn btn-sm" onClick={addMockRule} style={{ marginTop: 6 }}>
          + 添加 Mock 规则
        </button>
      </div>

      <button
        className="btn btn-primary"
        onClick={onSave}
        disabled={!dirty}
        style={{ opacity: dirty ? 1 : 0.4, marginTop: 6 }}
      >
        保存配置
      </button>
    </div>
  );
}

function MockRuleCard({
  rule,
  onChange,
  onRemove,
}: {
  rule: MockRule;
  onChange: (patch: Partial<MockRule>) => void;
  onRemove: () => void;
}) {
  const [editorOpen, setEditorOpen] = useState(false);
  const bodyLabel = rule.mode === "respond" ? "响应体" : "改写片段";

  return (
    <div className={`mock-card ${rule.enabled ? "" : "mock-disabled"}`}>
      <div className="mock-row">
        <label className="toggle">
          <input
            type="checkbox"
            checked={rule.enabled}
            onChange={(e) => onChange({ enabled: e.target.checked })}
          />
          <span className="slider" />
        </label>
        <input
          className="mock-name"
          type="text"
          value={rule.name}
          onChange={(e) => onChange({ name: e.target.value })}
          placeholder="规则名"
        />
        <select
          value={rule.mode}
          onChange={(e) => onChange({ mode: e.target.value as MockRule["mode"] })}
          title="respond 不向服务端发请求；patch 照发请求再合并片段"
        >
          <option value="respond">直接应答</option>
          <option value="patch">局部改写</option>
        </select>
        <select
          value={rule.method}
          onChange={(e) => onChange({ method: e.target.value })}
          title="留空则匹配所有方法"
        >
          <option value="">全部方法</option>
          <option value="GET">GET</option>
          <option value="POST">POST</option>
          <option value="PUT">PUT</option>
          <option value="DELETE">DELETE</option>
          <option value="PATCH">PATCH</option>
        </select>
        <button className="btn-bp-remove" onClick={onRemove} title="删除">
          ×
        </button>
      </div>

      <input
        className="mock-pattern"
        type="text"
        value={rule.url_pattern}
        onChange={(e) => onChange({ url_pattern: e.target.value })}
        placeholder="URL 匹配（正则），例如 /api/v1/user/profile"
      />

      <div className="mock-row mock-row-fields">
        {rule.mode === "respond" && (
          <label className="mock-field">
            状态码
            <input
              type="number"
              value={rule.status}
              onChange={(e) => onChange({ status: Number(e.target.value) || 200 })}
            />
          </label>
        )}
        <label className="mock-field" title="0 表示不限次数">
          次数
          <input
            type="number"
            min={0}
            value={rule.times}
            onChange={(e) => onChange({ times: Math.max(0, Number(e.target.value) || 0) })}
          />
        </label>
        <label className="mock-field">
          延迟 ms
          <input
            type="number"
            min={0}
            value={rule.delay_ms}
            onChange={(e) => onChange({ delay_ms: Math.max(0, Number(e.target.value) || 0) })}
          />
        </label>
        <label className="checkbox-row" title="关掉则以明文返回，不经过加解密后端">
          <input
            type="checkbox"
            checked={rule.encrypt}
            onChange={(e) => onChange({ encrypt: e.target.checked })}
          />
          加密
        </label>
        <button className="btn btn-sm" onClick={() => setEditorOpen(true)}>
          ✎ {bodyLabel}
          {rule.body ? "" : "（空）"}
        </button>
      </div>

      {editorOpen && (
        <JsonEditorModal
          value={rule.body}
          title={`${bodyLabel}编辑 — ${rule.name}`}
          onConfirm={(value) => {
            onChange({ body: value });
            setEditorOpen(false);
          }}
          onCancel={() => setEditorOpen(false)}
        />
      )}
    </div>
  );
}
