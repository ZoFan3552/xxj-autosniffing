use crate::adb_manager::AdbManager;
use crate::config::Config;
use crate::models::{InterceptAction, InterceptResponse};
use crate::proxy_runner::{find_python, InterceptBridge, ProxyRunner};
use parking_lot::Mutex;
use serde::Serialize;
use std::process::Command;
use std::sync::Arc;
use tauri::{Emitter, State};

#[derive(Serialize)]
pub struct EnvironmentCheckItem {
    pub key: String,
    pub label: String,
    pub ok: bool,
    pub detail: String,
    pub hint: Option<String>,
}

#[derive(Serialize)]
pub struct EnvironmentCheckResult {
    pub all_ok: bool,
    pub items: Vec<EnvironmentCheckItem>,
}

pub struct AppState {
    pub adb: Mutex<Option<AdbManager>>,
    pub proxy: Mutex<Option<ProxyRunner>>,
    pub bridge: Arc<InterceptBridge>,
    pub config: Mutex<Config>,
}

fn check_command_exists(command: &str, args: &[&str]) -> bool {
    let mut cmd = Command::new(command);
    cmd.args(args);
    cmd.stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null());

    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x08000000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }

    cmd.status().map(|s| s.success()).unwrap_or(false)
}

fn check_python_module(python: &str, module: &str) -> bool {
    let mut cmd = Command::new(python);
    cmd.args(["-c", &format!("import {}", module)]);
    cmd.stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null());

    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x08000000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }

    cmd.status().map(|s| s.success()).unwrap_or(false)
}

fn build_env_check_items() -> Vec<EnvironmentCheckItem> {
    let mut items = Vec::new();

    let python = find_python();
    items.push(EnvironmentCheckItem {
        key: "python".to_string(),
        label: "Python".to_string(),
        ok: python.is_some(),
        detail: python
            .as_ref()
            .map(|p| format!("found in PATH: {}", p))
            .unwrap_or_else(|| "python/python3 not found in PATH".to_string()),
        hint: python
            .is_none()
            .then_some("Install Python and ensure python or python3 is available in PATH.".to_string()),
    });

    let adb_ok = check_command_exists("adb", &["version"]);
    items.push(EnvironmentCheckItem {
        key: "adb".to_string(),
        label: "ADB".to_string(),
        ok: adb_ok,
        detail: if adb_ok {
            "adb command is available".to_string()
        } else {
            "adb not found in PATH".to_string()
        },
        hint: (!adb_ok)
            .then_some("Install Android Platform Tools and add adb to PATH.".to_string()),
    });

    if let Some(ref py) = python {
        let mitmproxy_ok = check_python_module(py, "mitmproxy");
        items.push(EnvironmentCheckItem {
            key: "mitmproxy".to_string(),
            label: "Python module: mitmproxy".to_string(),
            ok: mitmproxy_ok,
            detail: if mitmproxy_ok {
                "mitmproxy module import succeeded".to_string()
            } else {
                "mitmproxy module import failed".to_string()
            },
            hint: (!mitmproxy_ok)
                .then_some(format!("Run: {} -m pip install mitmproxy", py)),
        });

        let httpx_ok = check_python_module(py, "httpx");
        items.push(EnvironmentCheckItem {
            key: "httpx".to_string(),
            label: "Python module: httpx".to_string(),
            ok: httpx_ok,
            detail: if httpx_ok {
                "httpx module import succeeded".to_string()
            } else {
                "httpx module import failed".to_string()
            },
            hint: (!httpx_ok)
                .then_some(format!("Run: {} -m pip install httpx", py)),
        });
    } else {
        items.push(EnvironmentCheckItem {
            key: "mitmproxy".to_string(),
            label: "Python module: mitmproxy".to_string(),
            ok: false,
            detail: "skipped because python is missing".to_string(),
            hint: Some("Install Python first, then install mitmproxy.".to_string()),
        });
        items.push(EnvironmentCheckItem {
            key: "httpx".to_string(),
            label: "Python module: httpx".to_string(),
            ok: false,
            detail: "skipped because python is missing".to_string(),
            hint: Some("Install Python first, then install httpx.".to_string()),
        });
    }

    items
}

#[tauri::command]
pub fn check_environment() -> Result<EnvironmentCheckResult, String> {
    let items = build_env_check_items();
    let all_ok = items.iter().all(|i| i.ok);
    Ok(EnvironmentCheckResult { all_ok, items })
}

// ── Config ──

#[tauri::command]
pub fn get_config(state: State<'_, AppState>) -> Result<Config, String> {
    Ok(state.config.lock().clone())
}

#[tauri::command]
pub fn set_config(
    data: serde_json::Value,
    state: State<'_, AppState>,
) -> Result<serde_json::Value, String> {
    let (encrypt_url, decrypt_url, capture_hosts, breakpoints, proxy_port, proxy_port_changed) = {
        let mut cfg = state.config.lock();
        let old_proxy_port = cfg.proxy_port;

        cfg.merge_from(&data)?;
        cfg.save()?;

        (
            cfg.encrypt_url.clone(),
            cfg.decrypt_url.clone(),
            cfg.capture_hosts.clone(),
            serde_json::to_value(&cfg.breakpoints)
                .unwrap_or(serde_json::Value::Array(vec![])),
            cfg.proxy_port,
            cfg.proxy_port != old_proxy_port,
        )
    };

    if !state.bridge.send_command(serde_json::json!({
        "command": "update_config",
        "encrypt_url": encrypt_url,
        "decrypt_url": decrypt_url,
        "capture_hosts": capture_hosts,
        "breakpoints": breakpoints,
    })) {
        log::warn!("[config] failed to push config to Python subprocess (not running?)");
    }

    if proxy_port_changed {
        let proxy_running = state
            .proxy
            .lock()
            .as_ref()
            .map(|runner| runner.is_alive())
            .unwrap_or(false);
        if !proxy_running {
            if let Some(ref adb) = *state.adb.lock() {
                adb.set_proxy_port(proxy_port);
            }
        }
    }

    Ok(serde_json::json!({"ok": true}))
}

// ── Proxy ──

#[tauri::command]
pub fn proxy_start(
    app: tauri::AppHandle,
    state: State<'_, AppState>,
) -> Result<serde_json::Value, String> {
    let env_result = check_environment()?;
    if !env_result.all_ok {
        return Ok(serde_json::json!({
            "ok": false,
            "reason": "运行环境检查未通过，请先修复后再启动",
            "env": env_result,
        }));
    }

    let mut proxy_guard = state.proxy.lock();
    if let Some(ref runner) = *proxy_guard {
        if runner.is_alive() {
            return Ok(serde_json::json!({"ok": false, "reason": "already running"}));
        }
    }
    proxy_guard.take();

    let cfg = state.config.lock().clone();

    let runner = match ProxyRunner::start(&cfg, app.clone(), state.bridge.clone()) {
        Ok(r) => r,
        Err(e) => {
            let _ = app.emit("proxy_error", serde_json::json!({"error": e}));
            let _ = app.emit("proxy_status", serde_json::json!({"running": false}));
            return Ok(serde_json::json!({"ok": false, "reason": e}));
        }
    };
    *proxy_guard = Some(runner);

    // Setup ADB proxy after mitmproxy is confirmed to be started.
    let mut adb_setup_failed = false;
    if let Some(ref adb) = *state.adb.lock() {
        if let Some(device) = AdbManager::get_connected_device() {
            adb_setup_failed = !adb.setup_proxy(&device);
            if adb_setup_failed {
                let _ = app.emit(
                    "proxy_error",
                    serde_json::json!({"error": "ADB proxy setup failed; check adb connection and permissions"}),
                );
            }
        }
    }

    Ok(serde_json::json!({"ok": true, "adb_setup_ok": !adb_setup_failed}))
}

#[tauri::command]
pub fn proxy_stop(state: State<'_, AppState>) -> Result<serde_json::Value, String> {
    let mut proxy_guard = state.proxy.lock();
    if let Some(mut runner) = proxy_guard.take() {
        runner.stop();
    }
    if let Some(ref adb) = *state.adb.lock() {
        if let Some(device) = adb.last_known_device() {
            adb.teardown_proxy(&device);
        }
    }
    Ok(serde_json::json!({"ok": true}))
}

// ── Intercept ──

#[tauri::command]
pub fn intercept_respond(
    payload: InterceptResponse,
    state: State<'_, AppState>,
) -> Result<serde_json::Value, String> {
    let action_str = match &payload.action {
        InterceptAction::Pass => "pass",
        InterceptAction::Abort => "abort",
    };
    log::info!(
        "[intercept_respond] flow_id={} action={} body_len={}",
        payload.flow_id,
        action_str,
        payload.body.as_ref().map(|b| b.len()).unwrap_or(0)
    );
    let ok = state
        .bridge
        .respond(&payload.flow_id, action_str.to_string(), payload.body);
    log::info!("[intercept_respond] bridge.respond returned ok={}", ok);
    Ok(serde_json::json!({"ok": ok}))
}

// ── ADB ──

#[tauri::command]
pub fn adb_status(_state: State<'_, AppState>) -> Result<serde_json::Value, String> {
    let device = AdbManager::get_connected_device();
    Ok(serde_json::json!({"device": device}))
}
