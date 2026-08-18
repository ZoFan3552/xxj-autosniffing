mod adb_manager;
mod commands;
mod config;
mod models;
mod proxy_runner;

use commands::AppState;
use parking_lot::Mutex;
use proxy_runner::InterceptBridge;
use std::sync::Arc;
use tauri::Manager;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info")).init();

    let bridge = Arc::new(InterceptBridge::new());
    let cfg = config::Config::load();

    let app = tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .manage(AppState {
            adb: Mutex::new(None),
            proxy: Mutex::new(None),
            bridge,
            config: Mutex::new(cfg.clone()),
        })
        .setup(|app| {
            let state = app.state::<AppState>();
            let proxy_port = state.config.lock().proxy_port;
            let adb = adb_manager::AdbManager::new(proxy_port);
            adb.start_polling(app.handle().clone());
            *state.adb.lock() = Some(adb);
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            commands::get_config,
            commands::set_config,
            commands::proxy_start,
            commands::proxy_stop,
            commands::intercept_respond,
            commands::outbound_send,
            commands::ws_replay_arm,
            commands::adb_status,
        ])
        .build(tauri::generate_context!())
        .expect("error while building tauri application");

    app.run(|handle, event| {
        if let tauri::RunEvent::Exit = event {
            let state = handle.state::<AppState>();
            // Stop proxy
            {
                let mut proxy_guard = state.proxy.lock();
                if let Some(mut runner) = proxy_guard.take() {
                    runner.stop();
                }
            }
            // Teardown ADB proxy using last known device (may already be disconnected)
            {
                let mut adb_guard = state.adb.lock();
                if let Some(adb) = adb_guard.take() {
                    if let Some(device) = adb.last_known_device() {
                        adb.teardown_proxy(&device);
                    }
                    adb.stop_polling();
                }
            }
        }
    });
}
