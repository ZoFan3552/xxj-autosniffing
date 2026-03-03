import { useEffect, useState } from "react";
import type { Config, BreakpointRule } from "../types";

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
