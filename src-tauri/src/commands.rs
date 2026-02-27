use crate::adb_manager::AdbManager;
use crate::config::Config;
use crate::models::{InterceptAction, InterceptResponse};
use crate::proxy_runner::{InterceptBridge, ProxyRunner};
use parking_lot::Mutex;
use std::sync::Arc;
use tauri::{Emitter, State};

pub struct AppState {
    pub adb: Mutex<Option<AdbManager>>,
    pub proxy: Mutex<Option<ProxyRunner>>,
    pub bridge: Arc<InterceptBridge>,
    pub config: Mutex<Config>,
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
    let mut cfg = state.config.lock();
    cfg.merge_from(&data)?;
    cfg.save()?;

    // Push config update to running Python subprocess in real-time.
    // BreakpointRule derives Serialize so we can use it directly.
    let bp_json = serde_json::to_value(&cfg.breakpoints).unwrap_or(serde_json::Value::Array(vec![]));

    state.bridge.send_command(serde_json::json!({
        "command": "update_config",
        "encrypt_url": cfg.encrypt_url,
        "decrypt_url": cfg.decrypt_url,
        "capture_hosts": cfg.capture_hosts,
        "breakpoints": bp_json,
    }));

    Ok(serde_json::json!({"ok": true}))
}

// ── Proxy ──

#[tauri::command]
pub fn proxy_start(
    app: tauri::AppHandle,
    state: State<'_, AppState>,
) -> Result<serde_json::Value, String> {
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
    if let Some(ref adb) = *state.adb.lock() {
        if let Some(device) = AdbManager::get_connected_device() {
            adb.setup_proxy(&device);
        }
    }

    Ok(serde_json::json!({"ok": true}))
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
