use parking_lot::Mutex;
use std::process::Command;
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};
use std::time::Duration;
use tauri::{AppHandle, Emitter};

pub struct AdbManager {
    proxy_port: u16,
    current_device: Arc<Mutex<Option<String>>>,
    polling: Arc<AtomicBool>,
}

impl AdbManager {
    pub fn new(proxy_port: u16) -> Self {
        Self {
            proxy_port,
            current_device: Arc::new(Mutex::new(None)),
            polling: Arc::new(AtomicBool::new(false)),
        }
    }

    fn run_adb(args: &[&str]) -> (i32, String, String) {
        match Command::new("adb").args(args).output() {
            Ok(output) => {
                let code = output.status.code().unwrap_or(-1);
                let stdout = String::from_utf8_lossy(&output.stdout).trim().to_string();
                let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
                (code, stdout, stderr)
            }
            Err(_) => (-1, String::new(), "adb not found".to_string()),
        }
    }

    pub fn get_connected_device() -> Option<String> {
        let (code, out, _stderr) = Self::run_adb(&["devices"]);
        if code != 0 {
            return None;
        }
        for line in out.lines().skip(1) {
            if line.contains("\tdevice") {
                return line.split('\t').next().map(|s| s.to_string());
            }
        }
        None
    }

    /// Return the last known device from the polling cache.
    /// Useful for teardown when the device may already be disconnected.
    pub fn last_known_device(&self) -> Option<String> {
        self.current_device.lock().clone()
    }

    pub fn setup_proxy(&self, device: &str) {
        let port_str = format!("127.0.0.1:{}", self.proxy_port);
        let tcp = format!("tcp:{}", self.proxy_port);
        let (_, _, err) = Self::run_adb(&[
            "-s", device, "shell", "settings", "put", "global", "http_proxy", &port_str,
        ]);
        if !err.is_empty() {
            log::warn!("[adb] setup_proxy http_proxy stderr: {}", err);
        }
        let (_, _, err) = Self::run_adb(&["-s", device, "reverse", &tcp, &tcp]);
        if !err.is_empty() {
            log::warn!("[adb] setup_proxy reverse stderr: {}", err);
        }
    }

    pub fn teardown_proxy(&self, device: &str) {
        let tcp = format!("tcp:{}", self.proxy_port);
        let (_, _, err) = Self::run_adb(&[
            "-s", device, "shell", "settings", "put", "global", "http_proxy", ":0",
        ]);
        if !err.is_empty() {
            log::warn!("[adb] teardown_proxy http_proxy stderr: {}", err);
        }
        let (_, _, err) = Self::run_adb(&["-s", device, "reverse", "--remove", &tcp]);
        if !err.is_empty() {
            log::warn!("[adb] teardown_proxy reverse stderr: {}", err);
        }
    }

    pub fn start_polling(&self, app: AppHandle) {
        if self.polling.swap(true, Ordering::SeqCst) {
            return; // already polling
        }
        let current = self.current_device.clone();
        let polling = self.polling.clone();

        std::thread::spawn(move || {
            while polling.load(Ordering::SeqCst) {
                let device = Self::get_connected_device();
                let mut cur = current.lock();
                if *cur != device {
                    *cur = device.clone();
                    let _ = app.emit(
                        "adb_status",
                        serde_json::json!({
                            "device": device.as_deref().unwrap_or("")
                        }),
                    );
                }
                drop(cur);
                std::thread::sleep(Duration::from_secs(3));
            }
        });
    }

    pub fn stop_polling(&self) {
        self.polling.store(false, Ordering::SeqCst);
    }

}
