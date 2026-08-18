import { useMemo, useState } from "react";
import type { WsConn, WsFrame } from "../types";
import { formatSize } from "../utils/format";
import { ResizableSplit } from "./ResizableSplit";

/**
 * WebSocket 面板：左侧列出本次代理运行中的连接，右侧列出所选连接的帧。
 *
 * 「装填回放」把这条连接录到的帧交给代理，下一条握手在同一 path 上的连接会被回放
 * 顶替：上行帧不再发给服务端，下行帧按录制时的相对时刻注回客户端。
 */
export function WsPanel({
  conns,
  frames,
  onArmReplay,
  armedPath,
  onClear,
}: {
  conns: WsConn[];
  frames: WsFrame[];
  onArmReplay: (path: string, frames: WsFrame[]) => void;
  armedPath: string | null;
  onClear: () => void;
}) {
  const [selectedConn, setSelectedConn] = useState<number | null>(null);

  const connFrames = useMemo(
    () => (selectedConn == null ? [] : frames.filter((f) => f.conn === selectedConn)),
    [frames, selectedConn],
  );
  const selected = conns.find((c) => c.conn === selectedConn) ?? null;

  if (conns.length === 0) {
    return (
      <div className="empty-state">
        <div className="empty-icon">🔌</div>
        <p>暂无 WebSocket 连接</p>
        <p style={{ fontSize: 10 }}>抓包 Host 列表内的 WS 握手会出现在这里</p>
      </div>
    );
  }

  return (
    <div className="ws-panel">
      <div className="traffic-toolbar">
        <span className="traffic-count">
          {conns.length} 条连接 / {frames.length} 帧
        </span>
        {armedPath && (
          <span className="ws-armed">
            已装填回放：{armedPath}
            <button className="btn btn-sm" onClick={() => onArmReplay(armedPath, [])}>
              取消
            </button>
          </span>
        )}
        <button className="btn btn-sm" onClick={onClear}>清空</button>
      </div>

      <ResizableSplit direction="horizontal" initialRatio={0.32} minRatio={0.2} maxRatio={0.6}>
        <div className="ws-conn-list">
          {conns.map((conn) => (
            <button
              key={conn.conn}
              className={`ws-conn-item ${conn.conn === selectedConn ? "active" : ""}`}
              onClick={() => setSelectedConn(conn.conn)}
            >
              <div className="ws-conn-head">
                <span className={`status-dot ${conn.state === "open" ? "on" : "off"}`} />
                <span className="ws-conn-no">#{conn.conn}</span>
                <span className="ws-conn-count">{conn.frames} 帧</span>
                {conn.replaying && <span className="ws-tag">回放中</span>}
              </div>
              <div className="ws-conn-url" title={conn.url}>{conn.path || conn.url}</div>
              <div className="ws-conn-host">{conn.host}</div>
            </button>
          ))}
        </div>

        {selected ? (
          <div className="ws-frame-pane">
            <div className="ws-frame-toolbar">
              <span className="ws-frame-url" title={selected.url}>{selected.url}</span>
              <button
                className="btn btn-sm btn-primary"
                onClick={() => onArmReplay(selected.path, connFrames)}
                disabled={connFrames.length === 0}
                title="下一条握手在同一 path 的连接会被这批帧顶替"
              >
                ▶ 装填回放（{connFrames.length} 帧）
              </button>
            </div>
            <div className="ws-frame-list">
              {connFrames.map((frame) => (
                <FrameRow key={`${frame.conn}-${frame.seq}`} frame={frame} />
              ))}
            </div>
          </div>
        ) : (
          <div className="empty-state">
            <p>选择左侧的连接查看帧</p>
          </div>
        )}
      </ResizableSplit>
    </div>
  );
}

function FrameRow({ frame }: { frame: WsFrame }) {
  const [expanded, setExpanded] = useState(false);
  const preview = frame.payload.length > 120 ? frame.payload.slice(0, 120) + "…" : frame.payload;

  return (
    <div className={`ws-frame ${frame.dir}`} onClick={() => setExpanded((v) => !v)}>
      <div className="ws-frame-head">
        <span className="ws-frame-dir">{frame.dir === "up" ? "↑ 上行" : "↓ 下行"}</span>
        <span className="ws-frame-t">{frame.t_ms.toFixed(0)} ms</span>
        <span className="ws-frame-type">{frame.type === "text" ? "文本" : "二进制"}</span>
        <span className="ws-frame-size">{formatSize(frame.size)}</span>
        {frame.injected && <span className="ws-tag">注入</span>}
      </div>
      <pre className="ws-frame-payload">{expanded ? frame.payload : preview}</pre>
    </div>
  );
}
