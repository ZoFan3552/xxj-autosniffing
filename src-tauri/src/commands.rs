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
    let mut proxy_guard = state.proxy.lock();
    if let Some(ref runner) = *proxy_guard {
        if runner.is_alive() {
            return Ok(serde_json::json!({"ok": false, "reason": "already running"}));
        }
    }
    // Fully stop the old runner (if any) before starting a new one.
    // This prevents a background stop thread from clearing the new bridge stdin.
    if let Some(mut old_runner) = proxy_guard.take() {
        old_runner.stop();
    }

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
pub fn proxy_stop(
    app: tauri::AppHandle,
    state: State<'_, AppState>,
) -> Result<serde_json::Value, String> {
    // Take the runner out while holding the lock only briefly.
    let runner_opt = state.proxy.lock().take();

    // Stop synchronously to guarantee the bridge stdin is fully cleared
    // before any subsequent proxy_start can set a new one.
    // The stop() implementation already has timeouts so this won't hang.
    if let Some(mut runner) = runner_opt {
        runner.stop();
    }
    // Ensure proxy_status is emitted even if the runner was already dead.
    let _ = app.emit("proxy_status", serde_json::json!({"running": false}));

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
