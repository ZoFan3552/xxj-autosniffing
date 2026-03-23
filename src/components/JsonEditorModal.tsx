import { useState, useCallback, useRef, useEffect } from "react";
import Editor, { type OnMount } from "@monaco-editor/react";

/**
 * 全屏 JSON 编辑弹窗，基于 Monaco Editor。
 * 支持 JSON 语法高亮、自动格式化、错误提示。
 */
export function JsonEditorModal({
  value,
  title,
  onConfirm,
  onCancel,
}: {
  value: string;
  title: string;
  onConfirm: (newValue: string) => void;
  onCancel: () => void;
}) {
  const [currentValue, setCurrentValue] = useState(value);
  const [jsonError, setJsonError] = useState<string | null>(null);
  const editorRef = useRef<Parameters<OnMount>[0] | null>(null);

  // ESC 关闭
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCancel();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onCancel]);

  const handleMount: OnMount = (editor, monaco) => {
    editorRef.current = editor;
    // 配置 JSON 诊断
    monaco.languages.json.jsonDefaults.setDiagnosticsOptions({
      validate: true,
      allowComments: false,
      trailingCommas: "error",
    });
    editor.focus();
  };

  const handleChange = useCallback((val: string | undefined) => {
    const v = val ?? "";
    setCurrentValue(v);
    try {
      JSON.parse(v);
      setJsonError(null);
    } catch (e) {
      setJsonError(e instanceof Error ? e.message : "JSON 格式错误");
    }
  }, []);

  const handleFormat = useCallback(() => {
    try {
      const formatted = JSON.stringify(JSON.parse(currentValue), null, 2);
      setCurrentValue(formatted);
      setJsonError(null);
      // 同步到 Monaco
      if (editorRef.current) {
        editorRef.current.setValue(formatted);
      }
    } catch {
      setJsonError("JSON 格式错误，无法格式化");
    }
  }, [currentValue]);

  const handleMinify = useCallback(() => {
    try {
      const minified = JSON.stringify(JSON.parse(currentValue));
      setCurrentValue(minified);
      setJsonError(null);
      if (editorRef.current) {
        editorRef.current.setValue(minified);
      }
    } catch {
      setJsonError("JSON 格式错误，无法压缩");
    }
  }, [currentValue]);

  return (
    <div className="json-modal-overlay" onClick={onCancel}>
      <div className="json-modal" onClick={(e) => e.stopPropagation()}>
        <div className="json-modal-header">
          <span className="json-modal-title">{title}</span>
          <div className="json-modal-toolbar">
            {jsonError && <span className="json-modal-error">{jsonError}</span>}
            <button className="btn btn-sm" onClick={handleFormat}>格式化</button>
            <button className="btn btn-sm" onClick={handleMinify}>压缩</button>
          </div>
          <button className="json-modal-close" onClick={onCancel}>✕</button>
        </div>
        <div className="json-modal-editor">
          <Editor
            height="100%"
            defaultLanguage="json"
            value={currentValue}
            onChange={handleChange}
            onMount={handleMount}
            theme="json-light"
            beforeMount={(monaco) => {
              monaco.editor.defineTheme("json-light", {
                base: "vs",
                inherit: true,
                rules: [],
                colors: {
                  "editor.background": "#FFFBFE",
                  "editor.foreground": "#1C1B1F",
                  "editorLineNumber.foreground": "#79747E",
                  "editorLineNumber.activeForeground": "#49454F",
                  "editor.selectionBackground": "#EADDFF",
                  "editor.lineHighlightBackground": "#F7F2FA",
                  "editorCursor.foreground": "#6750A4",
                  "editorIndentGuide.background": "#CAC4D0",
                  "editorBracketMatch.background": "#EADDFF80",
                  "editorBracketMatch.border": "#6750A4",
                },
              });
            }}
            options={{
              fontSize: 13,
              fontFamily: "'JetBrains Mono', 'Cascadia Code', 'Consolas', monospace",
              minimap: { enabled: false },
              lineNumbers: "on",
              scrollBeyondLastLine: false,
              wordWrap: "on",
              tabSize: 2,
              automaticLayout: true,
              bracketPairColorization: { enabled: true },
              formatOnPaste: true,
              padding: { top: 8, bottom: 8 },
              renderLineHighlight: "line",
              smoothScrolling: true,
              cursorBlinking: "smooth",
              cursorSmoothCaretAnimation: "on",
            }}
          />
        </div>
        <div className="json-modal-footer">
          <button className="btn btn-sm" onClick={onCancel}>取消</button>
          <button className="btn btn-primary btn-sm" onClick={() => onConfirm(currentValue)}>
            确认修改
          </button>
        </div>
      </div>
    </div>
  );
}
