import { useRef, useCallback, useEffect, useState } from "react";

/**
 * 可拖拽分割的容器，支持上下（vertical）或左右（horizontal）方向。
 * 通过拖拽分割条来调整两个子区域的大小比例。
 */
export function ResizableSplit({
  direction = "vertical",
  initialRatio = 0.5,
  minRatio = 0.15,
  maxRatio = 0.85,
  children,
}: {
  direction?: "vertical" | "horizontal";
  /** 第一个面板占比 0~1 */
  initialRatio?: number;
  minRatio?: number;
  maxRatio?: number;
  children: [React.ReactNode, React.ReactNode];
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [ratio, setRatio] = useState(initialRatio);
  const dragging = useRef(false);

  const onMouseDown = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault();
      dragging.current = true;
      document.body.style.cursor =
        direction === "vertical" ? "row-resize" : "col-resize";
      document.body.style.userSelect = "none";
    },
    [direction],
  );

  useEffect(() => {
    const onMouseMove = (e: MouseEvent) => {
      if (!dragging.current || !containerRef.current) return;
      const rect = containerRef.current.getBoundingClientRect();
      let newRatio: number;
      if (direction === "vertical") {
        newRatio = (e.clientY - rect.top) / rect.height;
      } else {
        newRatio = (e.clientX - rect.left) / rect.width;
      }
      setRatio(Math.min(maxRatio, Math.max(minRatio, newRatio)));
    };

    const onMouseUp = () => {
      if (!dragging.current) return;
      dragging.current = false;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };

    window.addEventListener("mousemove", onMouseMove);
    window.addEventListener("mouseup", onMouseUp);
    return () => {
      window.removeEventListener("mousemove", onMouseMove);
      window.removeEventListener("mouseup", onMouseUp);
    };
  }, [direction, minRatio, maxRatio]);

  const isVertical = direction === "vertical";
  const pct1 = `${ratio * 100}%`;
  const pct2 = `${(1 - ratio) * 100}%`;

  return (
    <div
      ref={containerRef}
      className="resizable-split"
      style={{
        display: "flex",
        flexDirection: isVertical ? "column" : "row",
        height: "100%",
        width: "100%",
        overflow: "hidden",
      }}
    >
      <div
        className="resizable-pane"
        style={{
          [isVertical ? "height" : "width"]: `calc(${pct1} - 3px)`,
          overflow: "hidden",
          display: "flex",
          flexDirection: "column",
        }}
      >
        {children[0]}
      </div>
      <div
        className={`resizable-divider ${isVertical ? "divider-h" : "divider-v"}`}
        onMouseDown={onMouseDown}
      />
      <div
        className="resizable-pane"
        style={{
          [isVertical ? "height" : "width"]: `calc(${pct2} - 3px)`,
          overflow: "hidden",
          display: "flex",
          flexDirection: "column",
        }}
      >
        {children[1]}
      </div>
    </div>
  );
}
