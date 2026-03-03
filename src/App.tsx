import { useEffect, useState, useCallback, useMemo, useRef } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import "react-json-view-lite/dist/index.css";
import type {
  Config,
  RequestRecord,
  InterceptRequest,
} from "./types";
import { TrafficPanel } from "./components/TrafficPanel";
import { InterceptPanel } from "./components/InterceptPanel";
import { SettingsPanel } from "./components/SettingsPanel";
import { type DetailTab } from "./components/DetailPanel";
import "./App.css";

type Tab = "traffic" | "intercept" | "settings";
type ProxyStartResult = {
  ok: boolean;
  reason?: string;
  adb_setup_ok?: boolean;
};

const MAX_RECORDS = 2000;
const MAX_INTERCEPTS = 200;

function App({ onAppReady }: { onAppReady: () => void }) {
  const [tab, setTab] = useState<Tab>("traffic");
  const [proxyRunning, setProxyRunning] = useState(false);
  const [proxyLoading, setProxyLoading] = useState(false);
  const [adbDevice, setAdbDevice] = useState<string | null>(null);
  const [records, setRecords] = useState<RequestRecord[]>([]);
  // O(1) lookup index for flow_id → position in records array.
  const recordIndexRef = useRef<Map<string, number>>(new Map());
  const [intercepts, setIntercepts] = useState<InterceptRequest[]>([]);
  const [selected, setSelected] = useState<RequestRecord | null>(null);
  const [detailTab, setDetailTab] = useState<DetailTab>("overview");
  const [filter, setFilter] = useState("");
  const [config, setConfig] = useState<Config | null>(null);
  const [configDraft, setConfigDraft] = useState<Config | null>(null);
  const [interceptBodies, setInterceptBodies] = useState<Record<string, string>>({});
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [isDirty, setIsDirty] = useState(false);
  const bootHiddenRef = useRef(false);

  const markAppReady = useCallback(() => {
    if (bootHiddenRef.current) return;
    bootHiddenRef.current = true;
    onAppReady();
  }, [onAppReady]);

  useEffect(() => {
    let cancelled = false;

    Promise.all([
      invoke<Config>("get_config"),
      invoke<{ device: string | null }>("adb_status"),
    ])
      .then(([c, adb]) => {
        if (cancelled) return;
        setConfig(c);
        setConfigDraft(c);
        setAdbDevice(adb.device);
      })
      .catch((e) => {
        if (cancelled) return;
        setErrorMsg(String(e));
      })
      .finally(() => {
        if (!cancelled) {
          requestAnimationFrame(markAppReady);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [markAppReady]);

  useEffect(() => {
    let unlisteners: (() => void)[] = [];
    let cancelled = false;

    Promise.all([
      listen<RequestRecord>("record", (e) => {
        setRecords((prev) => {
          const indexMap = recordIndexRef.current;
          const existingIdx = indexMap.get(e.payload.flow_id);
          if (existingIdx !== undefined && existingIdx < prev.length) {
            const updated = [...prev];
            updated[existingIdx] = e.payload;
            return updated;
          }
          const next = [e.payload, ...prev].slice(0, MAX_RECORDS);
          // Rebuild index after prepend + possible truncation.
          const newMap = new Map<string, number>();
          for (let i = 0; i < next.length; i++) {
            newMap.set(next[i].flow_id, i);
          }
          recordIndexRef.current = newMap;
          return next;
        });
        setSelected((prev) =>
          prev && prev.flow_id === e.payload.flow_id ? e.payload : prev,
        );
      }),
      listen<InterceptRequest>("intercept", (e) => {
        setIntercepts((prev) => {
          if (prev.some((i) => i.flow_id === e.payload.flow_id)) return prev;
          return [e.payload, ...prev].slice(0, MAX_INTERCEPTS);
        });
        // Don't force-switch tab; the badge count on the sidebar alerts the user.
      }),
      listen<{ running: boolean }>("proxy_status", (e) => {
        setProxyRunning(e.payload.running);
        setProxyLoading(false);
      }),
      listen<{ error: string }>("proxy_error", (e) => {
        setErrorMsg(e.payload.error);
        setProxyLoading(false);
      }),
      listen<{ device: string }>("adb_status", (e) => {
        setAdbDevice(e.payload.device || null);
      }),
    ]).then((fns) => {
      if (cancelled) {
        fns.forEach((u) => u());
        return;
      }
      unlisteners = fns;
    });

    return () => {
      cancelled = true;
      unlisteners.forEach((u) => u());
    };
  }, []);

  const toggleProxy = useCallback(async () => {
    setProxyLoading(true);
    setErrorMsg(null);
    try {
      if (proxyRunning) {
        await invoke("proxy_stop");
      } else {
        const result = await invoke<ProxyStartResult>("proxy_start");
        if (!result.ok) {
          setErrorMsg(result.reason ?? "启动失败");
          setProxyLoading(false);
        }
      }
    } catch (e) {
      setErrorMsg(String(e));
      setProxyLoading(false);
    }
  }, [proxyRunning]);

  const clearRecords = useCallback(() => {
    setRecords([]);
    recordIndexRef.current = new Map();
    setSelected(null);
  }, []);

  const respondIntercept = useCallback(
    async (flowId: string, action: string, currentBody?: string) => {
      const body = action === "pass" ? (currentBody ?? null) : null;
      try {
        const result = await invoke<{ ok: boolean }>("intercept_respond", {
          payload: { flow_id: flowId, action, body },
        });
        if (!result.ok) {
          setErrorMsg("拦截响应发送失败，请检查代理进程是否正常运行");
          return;
        }
      } catch (e) {
        setErrorMsg(String(e));
        return;
      }
      setIntercepts((prev) => prev.filter((i) => i.flow_id !== flowId));
      setInterceptBodies((prev) => {
        const n = { ...prev };
        delete n[flowId];
        return n;
      });
    },
    [],
  );

  const saveConfig = useCallback(async () => {
    if (!configDraft) return;
    try {
      await invoke("set_config", { data: configDraft });
      setConfig(configDraft);
      setIsDirty(false);
    } catch (e) {
      setErrorMsg(String(e));
    }
  }, [configDraft]);

  const filteredRecords = useMemo(
    () =>
      filter
        ? records.filter(
            (r) =>
              r.url.toLowerCase().includes(filter.toLowerCase()) ||
              r.host.toLowerCase().includes(filter.toLowerCase()) ||
              r.method.toLowerCase().includes(filter.toLowerCase()),
          )
        : records,
    [records, filter],
  );

  useEffect(() => {
    if (!selected) return;
    if (!recordIndexRef.current.has(selected.flow_id)) {
      setSelected(null);
    }
  }, [records, selected]);

  return (
    <div className="app-layout">
      <div className="titlebar">
        <div className="titlebar-left">
          <span className="titlebar-logo">⚡ XXJ-SNIFF</span>
          <StatusBadge
            label="代理"
            on={proxyRunning}
            detail={proxyRunning ? `端口 ${config?.proxy_port ?? 8080}` : "未运行"}
          />
          <StatusBadge label="ADB" on={!!adbDevice} detail={adbDevice ?? "未连接"} />
        </div>
        <div className="titlebar-right">
          <button
            className={`btn btn-sm ${proxyRunning ? "btn-danger" : "btn-success"}`}
            onClick={toggleProxy}
            disabled={proxyLoading}
            style={{ opacity: proxyLoading ? 0.6 : 1 }}
          >
            {proxyLoading ? "..." : proxyRunning ? "■ 停止" : "▶ 启动"}
          </button>
        </div>
      </div>

      <div className="sidebar">
        <SidebarItem
          icon="📡"
          label="流量"
          active={tab === "traffic"}
          badge={records.length || undefined}
          onClick={() => setTab("traffic")}
        />
        <SidebarItem
          icon="🛡"
          label="拦截"
          active={tab === "intercept"}
          badge={intercepts.length || undefined}
          onClick={() => setTab("intercept")}
        />
        <SidebarItem
          icon="⚙"
          label="设置"
          active={tab === "settings"}
          onClick={() => setTab("settings")}
        />
      </div>

      <div className="main-area">
        {tab === "traffic" && (
          <TrafficPanel
            records={filteredRecords}
            selected={selected}
            onSelect={setSelected}
            filter={filter}
            onFilterChange={setFilter}
            onClear={clearRecords}
            detailTab={detailTab}
            onDetailTabChange={setDetailTab}
          />
        )}
        {tab === "intercept" && (
          <InterceptPanel
            intercepts={intercepts}
            bodies={interceptBodies}
            onBodyChange={(id, val) => setInterceptBodies((p) => ({ ...p, [id]: val }))}
            onRespond={respondIntercept}
          />
        )}
        {tab === "settings" && configDraft && (
          <SettingsPanel
            config={configDraft}
            onChange={(next) => {
              setConfigDraft(next);
              setIsDirty(true);
            }}
            onSave={saveConfig}
            dirty={isDirty}
          />
        )}
      </div>

      {errorMsg && (
        <div className="error-overlay" onClick={() => setErrorMsg(null)}>
          <div className="error-modal" onClick={(e) => e.stopPropagation()}>
            <div className="error-modal-title">⚠ 错误</div>
            <div className="error-modal-body">{errorMsg}</div>
            <div className="error-modal-footer">
              <button className="btn btn-sm" onClick={() => setErrorMsg(null)}>
                关闭
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function SidebarItem({
  icon,
  label,
  active,
  badge,
  onClick,
}: {
  icon: string;
  label: string;
  active: boolean;
  badge?: number;
  onClick: () => void;
}) {
  return (
    <button
      className={`sidebar-item ${active ? "active" : ""}`}
      onClick={onClick}
      title={label}
    >
      {icon}
      {badge !== undefined && badge > 0 && (
        <span className="badge">{badge > 99 ? "99+" : badge}</span>
      )}
    </button>
  );
}

function StatusBadge({ label, on, detail }: { label: string; on: boolean; detail: string }) {
  return (
    <span className="status-badge">
      <span className={`status-dot ${on ? "on" : "off"}`} />
      {label}：{detail}
    </span>
  );
}

export default App;
