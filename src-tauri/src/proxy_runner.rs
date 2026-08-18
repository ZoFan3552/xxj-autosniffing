use base64::Engine;
use parking_lot::Mutex;
use std::io::{BufRead, BufReader, Write};
use std::path::PathBuf;
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};
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

/// Returns the path to the bundled sidecar executable, if it exists next to the app binary.
fn find_sidecar() -> Option<PathBuf> {
    let exe_dir = std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|p| p.to_path_buf()))?;
    let name = if cfg!(target_os = "windows") {
        "addon_bridge.exe"
    } else {
        "addon_bridge"
    };
    let path = exe_dir.join(name);
    if path.exists() { Some(path) } else { None }
}

/// Runs mitmproxy as a Python subprocess (or bundled sidecar).
pub struct ProxyRunner {
    running: Arc<AtomicBool>,
    child: Arc<Mutex<Option<Child>>>,
    reader_thread: Option<std::thread::JoinHandle<()>>,
    stderr_thread: Option<std::thread::JoinHandle<()>>,
    bridge: Arc<InterceptBridge>,
    /// Temp script file written in Python-fallback mode; `None` when using sidecar.
    script_path: Option<PathBuf>,
}

impl ProxyRunner {
    pub fn start(
        cfg: &Config,
        app: AppHandle,
        bridge: Arc<InterceptBridge>,
    ) -> Result<Self, String> {
        let running = Arc::new(AtomicBool::new(true));

        let (mut cmd, script_path) = match find_sidecar() {
            Some(sidecar) => {
                log::info!("[proxy] Using bundled sidecar: {}", sidecar.display());
                (Command::new(&sidecar), None)
            }
            None => {
                log::info!("[proxy] Sidecar not found, falling back to system Python");
                let script_content = include_str!("python/addon_bridge.py");
                let path = std::env::temp_dir().join("xxj_addon_bridge.py");
                std::fs::write(&path, script_content)
                    .map_err(|e| format!("无法写入临时脚本：{}", e))?;
                let python = find_python().ok_or_else(|| {
                    "未找到可用运行环境：请安装 Python 并执行 pip install mitmproxy httpx，或先运行 python scripts/build_addon.py 构建内置可执行文件".to_string()
                })?;
                log::info!("[proxy] Using Python: {}", python);
                let mut c = Command::new(&python);
                c.arg(path.to_str().unwrap_or(""));
                (c, Some(path))
            }
        };

        // Non-sensitive config goes on the command line so it is in place before
        // mitmproxy binds the port; the sensitive half follows over stdin below.
        let launch_cfg = serde_json::json!({
            "proxy_port": cfg.proxy_port,
            "capture_hosts": cfg.capture_hosts,
            "breakpoints": cfg.breakpoints,
            "mock_rules": cfg.mock_rules,
        });
        let launch_cfg_json = serde_json::to_string(&launch_cfg).map_err(|e| e.to_string())?;

        cmd.arg("--config")
            .arg(&launch_cfg_json)
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
                "无法启动代理进程：{}\n请运行 python scripts/build_addon.py 构建内置可执行文件，或安装 Python 依赖：pip install mitmproxy httpx",
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
            if !bridge.send_command(cfg.update_command()) {
                log::warn!("[proxy] failed to push sensitive config via stdin update_config");
            }
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
                                    "ws_conn" => {
                                        let _ = app_handle.emit("ws_conn", &data);
                                    }
                                    "ws_frame" => {
                                        let _ = app_handle.emit("ws_frame", &data);
                                    }
                                    "outbound_result" => {
                                        let _ = app_handle.emit("outbound_result", &data);
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
            script_path,
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
            #[cfg(target_os = "windows")]
            {
                use std::os::windows::process::CommandExt;
                const CREATE_NO_WINDOW: u32 = 0x08000000;
                let pid = child.id();
                // /T kills the entire process tree (bootloader + spawned Python child).
                // This is required for PyInstaller --onefile sidecars on Windows.
                let _ = Command::new("taskkill")
                    .args(["/F", "/T", "/PID", &pid.to_string()])
                    .stdout(Stdio::null())
                    .stderr(Stdio::null())
                    .creation_flags(CREATE_NO_WINDOW)
                    .status();
                // Wait up to 3 s for the process to be fully reaped.
                let deadline = Instant::now() + Duration::from_secs(3);
                while Instant::now() < deadline {
                    match child.try_wait() {
                        Ok(Some(_)) => break,
                        Ok(None) => std::thread::sleep(Duration::from_millis(100)),
                        Err(_) => break,
                    }
                }
            }

            #[cfg(not(target_os = "windows"))]
            {
                let pid = child.id();
                let _ = Command::new("kill")
                    .arg("-TERM")
                    .arg(pid.to_string())
                    .stdout(Stdio::null())
                    .stderr(Stdio::null())
                    .status();

                let deadline = Instant::now() + Duration::from_secs(2);
                while Instant::now() < deadline {
                    match child.try_wait() {
                        Ok(Some(_)) => break,
                        Ok(None) => std::thread::sleep(Duration::from_millis(100)),
                        Err(_) => break,
                    }
                }

                if matches!(child.try_wait(), Ok(None)) {
                    let _ = Command::new("kill")
                        .arg("-KILL")
                        .arg(pid.to_string())
                        .stdout(Stdio::null())
                        .stderr(Stdio::null())
                        .status();
                }
            }

            let _ = child.wait();
        }

        if let Some(t) = self.reader_thread.take() {
            let _ = t.join();
        }
        if let Some(t) = self.stderr_thread.take() {
            let _ = t.join();
        }

        if let Some(ref path) = self.script_path {
            if let Err(e) = std::fs::remove_file(path.as_path()) {
                if e.kind() != std::io::ErrorKind::NotFound {
                    log::warn!(
                        "[proxy] failed to remove temp script {}: {}",
                        path.display(),
                        e
                    );
                }
            }
        }

        log::info!("[proxy] stopped");
    }
}

impl Drop for ProxyRunner {
    fn drop(&mut self) {
        self.stop();
    }
}

pub fn find_python() -> Option<String> {
    for name in &["python", "python3"] {
        let mut cmd = Command::new(name);
        cmd.arg("--version")
            .stdout(Stdio::null())
            .stderr(Stdio::null());

        #[cfg(target_os = "windows")]
        {
            use std::os::windows::process::CommandExt;
            const CREATE_NO_WINDOW: u32 = 0x08000000;
            cmd.creation_flags(CREATE_NO_WINDOW);
        }

        if cmd.status().map(|s| s.success()).unwrap_or(false) {
            return Some(name.to_string());
        }
    }
    None
}
