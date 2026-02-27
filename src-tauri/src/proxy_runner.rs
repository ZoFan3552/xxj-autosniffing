use base64::Engine;
use parking_lot::Mutex;
use std::io::{BufRead, BufReader, Write};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use tauri::{AppHandle, Emitter};

use crate::config::Config;

/// Sends intercept responses to the Python subprocess via stdin.
pub struct InterceptBridge {
    stdin: Mutex<Option<ChildStdin>>,
}

impl InterceptBridge {
    pub fn new() -> Self {
        Self {
            stdin: Mutex::new(None),
        }
    }

    pub fn set_stdin(&self, stdin: ChildStdin) {
        *self.stdin.lock() = Some(stdin);
    }

    pub fn clear_stdin(&self) {
        *self.stdin.lock() = None;
    }

    pub fn respond(&self, flow_id: &str, action: String, body: Option<String>) -> bool {
        log::info!("[bridge] respond flow_id={} action={}", flow_id, action);
        let body_b64 = body
            .map(|b| base64::engine::general_purpose::STANDARD.encode(b))
            .unwrap_or_default();
        let line = format!(
            "intercept_respond\t{}\t{}\t{}\n",
            flow_id, action, body_b64
        );
        self.send_line(&line)
    }

    /// Send any JSON command to the Python subprocess via stdin.
    pub fn send_command(&self, cmd: serde_json::Value) -> bool {
        if let Ok(line) = serde_json::to_string(&cmd) {
            let msg = format!("{}\n", line);
            return self.send_line(&msg);
        }
        false
    }

    fn send_line(&self, msg: &str) -> bool {
        let mut guard = self.stdin.lock();
        if let Some(ref mut stdin) = *guard {
            log::debug!("[bridge] sending to stdin: {} bytes", msg.len());
            if stdin.write_all(msg.as_bytes()).is_ok() {
                let _ = stdin.flush();
                log::debug!("[bridge] stdin write+flush OK");
                return true;
            }
            log::error!("[bridge] stdin write_all failed");
        } else {
            log::error!("[bridge] stdin is None, cannot send command");
        }
        false
    }
}

/// Runs mitmproxy as a Python subprocess.
pub struct ProxyRunner {
    running: Arc<AtomicBool>,
    child: Arc<Mutex<Option<Child>>>,
    reader_thread: Option<std::thread::JoinHandle<()>>,
    stderr_thread: Option<std::thread::JoinHandle<()>>,
    bridge: Arc<InterceptBridge>,
}

impl ProxyRunner {
    pub fn start(
        cfg: &Config,
        app: AppHandle,
        bridge: Arc<InterceptBridge>,
    ) -> Result<Self, String> {
        let running = Arc::new(AtomicBool::new(true));

        let script_content = include_str!("python/addon_bridge.py");
        let script_path = std::env::temp_dir().join("xxj_addon_bridge.py");
        std::fs::write(&script_path, script_content)
            .map_err(|e| format!("无法写入临时脚本：{}", e))?;

        let python = find_python().ok_or_else(|| {
            "未找到 Python，请确保 python 或 python3 已安装并在 PATH 中".to_string()
        })?;

        log::info!("[proxy] Using Python: {}", python);

        // Serialize the full config as JSON and pass via --config arg
        let config_json = serde_json::to_string(cfg).map_err(|e| e.to_string())?;

        let mut cmd = Command::new(&python);
        cmd.arg(script_path.to_str().unwrap_or(""))
            .arg("--config")
            .arg(&config_json)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .stdin(Stdio::piped());

        #[cfg(target_os = "windows")]
        {
            use std::os::windows::process::CommandExt;
            const CREATE_NO_WINDOW: u32 = 0x08000000;
            cmd.creation_flags(CREATE_NO_WINDOW);
        }

        let mut child = cmd.spawn().map_err(|e| {
            format!(
                "无法启动 Python 进程：{}\n请确保已安装 mitmproxy 和 httpx：\npip install mitmproxy httpx",
                e
            )
        })?;

        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| "无法获取 Python 进程 stdout".to_string())?;
        let stderr = child.stderr.take();
        let child_stdin = child.stdin.take();

        // Give the bridge the stdin handle for sending intercept responses
        if let Some(stdin) = child_stdin {
            bridge.set_stdin(stdin);
        }

        let child_arc = Arc::new(Mutex::new(Some(child)));
        let running_clone = running.clone();
        let app_handle = app.clone();

        // Stderr reader thread for logging — handle is saved so stop() can join it.
        let stderr_thread = stderr.map(|stderr| {
            std::thread::spawn(move || {
                let reader = BufReader::new(stderr);
                for line in reader.lines() {
                    if let Ok(line) = line {
                        if !line.trim().is_empty() {
                            log::warn!("[proxy-stderr] {}", line);
                        }
                    }
                }
            })
        });

        // Stdout reader thread — parses JSON event lines (lossy UTF-8)
        let reader_thread = std::thread::spawn(move || {
            let mut reader = BufReader::new(stdout);
            let mut buf = Vec::new();
            loop {
                buf.clear();
                match reader.read_until(b'\n', &mut buf) {
                    Ok(0) => break, // EOF
                    Ok(_) => {
                        let line = String::from_utf8_lossy(&buf).trim().to_string();
                        if line.is_empty() {
                            continue;
                        }
                        match serde_json::from_str::<serde_json::Value>(&line) {
                            Ok(msg) => {
                                let event =
                                    msg.get("event").and_then(|v| v.as_str()).unwrap_or("");
                                let data = msg
                                    .get("data")
                                    .cloned()
                                    .unwrap_or(serde_json::Value::Null);
                                match event {
                                    "record" => {
                                        let _ = app_handle.emit("record", &data);
                                    }
                                    "intercept" => {
                                        let _ = app_handle.emit("intercept", &data);
                                    }
                                    "status" => {
                                        // Sync the AtomicBool so is_alive() stays accurate.
                                        if let Some(r) = data.get("running").and_then(|v| v.as_bool()) {
                                            running_clone.store(r, Ordering::SeqCst);
                                        }
                                        let _ = app_handle.emit("proxy_status", &data);
                                    }
                                    "log" => {
                                        let level = data
                                            .get("level")
                                            .and_then(|v| v.as_str())
                                            .unwrap_or("info");
                                        let msg_str = data
                                            .get("msg")
                                            .and_then(|v| v.as_str())
                                            .unwrap_or("");
                                        match level {
                                            "error" => {
                                                log::error!("[proxy-py] {}", msg_str);
                                                // Surface errors to the frontend so the user
                                                // sees the real reason (e.g. port in use).
                                                let _ = app_handle.emit(
                                                    "proxy_error",
                                                    serde_json::json!({"error": msg_str}),
                                                );
                                            }
                                            "warn" => log::warn!("[proxy-py] {}", msg_str),
                                            "debug" => log::debug!("[proxy-py] {}", msg_str),
                                            _ => log::info!("[proxy-py] {}", msg_str),
                                        }
                                    }
                                    _ => {
                                        log::warn!("[proxy] unknown event: {}", event);
                                    }
                                }
                            }
                            Err(_) => {
                                // Non-JSON output, just log it
                                log::debug!("[proxy-stdout] {}", line);
                            }
                        }
                    }
                    Err(e) => {
                        log::error!("[proxy-stdout] read error: {}", e);
                        break;
                    }
                }
            }
            running_clone.store(false, Ordering::SeqCst);
            let _ = app_handle.emit(
                "proxy_status",
                serde_json::json!({"running": false}),
            );
            log::info!("[proxy] stdout reader finished");
        });

        // Do NOT pre-emit "running: true" here.
        // The Python SniffAddon.running() hook fires only after mitmproxy
        // has successfully bound the port; that event sends proxy_status:{running:true}.
        // On startup failure the Python finally-block sends proxy_status:{running:false}.

        Ok(Self {
            running,
            child: child_arc,
            reader_thread: Some(reader_thread),
            stderr_thread,
            bridge,
        })
    }

    pub fn is_alive(&self) -> bool {
        self.running.load(Ordering::SeqCst)
    }

    pub fn stop(&mut self) {
        log::info!("[proxy] stopping...");
        self.running.store(false, Ordering::SeqCst);
        self.bridge.clear_stdin();
        if let Some(mut child) = self.child.lock().take() {
            let _ = child.kill();
            let _ = child.wait();
        }
        if let Some(t) = self.reader_thread.take() {
            let _ = t.join();
        }
        if let Some(t) = self.stderr_thread.take() {
            let _ = t.join();
        }
        log::info!("[proxy] stopped");
    }
}

impl Drop for ProxyRunner {
    fn drop(&mut self) {
        self.stop();
    }
}

fn find_python() -> Option<String> {
    for name in &["python", "python3"] {
        if Command::new(name)
            .arg("--version")
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .is_ok()
        {
            return Some(name.to_string());
        }
    }
    None
}
