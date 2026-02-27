import { useRef } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import type { RequestRecord } from "../types";
import { formatSize, formatDuration, shortContentType } from "../utils/format";
import { DetailPanel, type DetailTab } from "./DetailPanel";

const ROW_HEIGHT = 26;

export function TrafficPanel({
  records,
  selected,
  onSelect,
  filter,
  onFilterChange,
  onClear,
  detailTab,
  onDetailTabChange,
}: {
  records: RequestRecord[];
  selected: RequestRecord | null;
  onSelect: (r: RequestRecord | null) => void;
  filter: string;
  onFilterChange: (v: string) => void;
  onClear: () => void;
  detailTab: DetailTab;
  onDetailTabChange: (t: DetailTab) => void;
}) {
  const parentRef = useRef<HTMLDivElement>(null);

  const rowVirtualizer = useVirtualizer({
    count: records.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: 15,
  });

  const virtualItems = rowVirtualizer.getVirtualItems();
  const totalSize = rowVirtualizer.getTotalSize();
  const paddingTop = virtualItems.length > 0 ? virtualItems[0].start : 0;
  const paddingBottom =
    virtualItems.length > 0 ? totalSize - virtualItems[virtualItems.length - 1].end : 0;

  return (
    <div className="traffic-panel">
      <div className="traffic-toolbar">
        <input
          type="text"
          placeholder="按 URL / Host / 方法过滤..."
          value={filter}
          onChange={(e) => onFilterChange(e.target.value)}
        />
        <span className="traffic-count">{records.length} 条</span>
        <button className="btn btn-sm" onClick={onClear}>
          清空
        </button>
      </div>
      <div className={`traffic-split ${selected ? "has-detail" : ""}`}>
        <div className="traffic-table-wrap" ref={parentRef}>
          {records.length === 0 ? (
            <div className="empty-state">
              <div className="empty-icon">📡</div>
              <p>暂无抓包数据</p>
              <p style={{ fontSize: 10 }}>启动代理后开始抓包</p>
            </div>
          ) : (
            <table className="traffic-table">
              <colgroup>
                <col style={{ width: 36 }} />
                <col style={{ width: 50 }} />
                <col style={{ width: 52 }} />
                <col style={{ width: 140 }} />
                <col />
                <col style={{ width: 80 }} />
                <col style={{ width: 60 }} />
                <col style={{ width: 60 }} />
              </colgroup>
              <thead>
                <tr>
                  <th>#</th>
                  <th>状态</th>
                  <th>方法</th>
                  <th className="col-host">Host</th>
                  <th className="col-path">Path</th>
                  <th>类型</th>
                  <th>大小</th>
                  <th>耗时</th>
                </tr>
              </thead>
              <tbody>
                {paddingTop > 0 && (
                  <tr>
                    <td colSpan={8} style={{ height: paddingTop, padding: 0, border: "none" }} />
                  </tr>
                )}
                {virtualItems.map((vRow) => {
                  const r = records[vRow.index];
                  return (
                    <tr
                      key={r.flow_id}
                      style={{ height: ROW_HEIGHT }}
                      className={`${selected?.flow_id === r.flow_id ? "selected" : ""} row-${r.status}`}
                      onClick={() => onSelect(selected?.flow_id === r.flow_id ? null : r)}
                    >
                      <td className="col-seq">{r.seq}</td>
                      <td>
                        <FlowStatus record={r} />
                      </td>
                      <td>
                        <span className={`method-tag ${r.method}`}>{r.method}</span>
                      </td>
                      <td className="col-host" title={r.host}>
                        {r.host}
                      </td>
                      <td className="col-path" title={r.path}>
                        {r.path}
                      </td>
                      <td className="col-type" title={r.response_content_type ?? ""}>
                        {shortContentType(r.response_content_type)}
                      </td>
                      <td className="col-size">{formatSize(r.response_size)}</td>
                      <td className="col-duration">{formatDuration(r.duration)}</td>
                    </tr>
                  );
                })}
                {paddingBottom > 0 && (
                  <tr>
                    <td colSpan={8} style={{ height: paddingBottom, padding: 0, border: "none" }} />
                  </tr>
                )}
              </tbody>
            </table>
          )}
        </div>
        {selected && (
          <DetailPanel record={selected} tab={detailTab} onTabChange={onDetailTabChange} />
        )}
      </div>
    </div>
  );
}

function FlowStatus({ record }: { record: RequestRecord }) {
  if (record.status === "pending") {
    return <span className="status-code status-pending">⏳</span>;
  }
  if (record.status === "error") {
    return <span className="status-code s5xx">ERR</span>;
  }
  if (record.status === "aborted") {
    return <span className="status-code status-aborted">ABT</span>;
  }
  return <StatusCode code={record.response_status} />;
}

function StatusCode({ code }: { code: number | null }) {
  if (code == null)
    return <span className="status-code" style={{ color: "var(--text-2)" }}>—</span>;
  let cls = "status-code";
  if (code >= 200 && code < 300) cls += " s2xx";
  else if (code >= 300 && code < 400) cls += " s3xx";
  else if (code >= 400 && code < 500) cls += " s4xx";
  else if (code >= 500) cls += " s5xx";
  return <span className={cls}>{code}</span>;
}
