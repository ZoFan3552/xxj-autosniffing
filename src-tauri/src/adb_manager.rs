use parking_lot::Mutex;
use std::process::Command;
use std::sync::{
    atomic::{AtomicBool, AtomicU16, Ordering},
    Arc,
};
use std::time::Duration;
use tauri::{AppHandle, Emitter};

pub struct AdbManager {
    proxy_port: Arc<AtomicU16>,
    current_device: Arc<Mutex<Option<String>>>,
    polling: Arc<AtomicBool>,
    poll_thread: Mutex<Option<std::thread::JoinHandle<()>>>,
}

impl AdbManager {
    pub fn new(proxy_port: u16) -> Self {
        Self {
            proxy_port: Arc::new(AtomicU16::new(proxy_port)),
            current_device: Arc::new(Mutex::new(None)),
            polling: Arc::new(AtomicBool::new(false)),
            poll_thread: Mutex::new(None),
        }
    }

    pub fn set_proxy_port(&self, port: u16) {
        self.proxy_port.store(port, Ordering::SeqCst);
    }

    fn run_adb(args: &[&str]) -> (i32, String, String) {
        let mut cmd = Command::new("adb");
        cmd.args(args);

        #[cfg(target_os = "windows")]
        {
            use std::os::windows::process::CommandExt;
            const CREATE_NO_WINDOW: u32 = 0x08000000;
            cmd.creation_flags(CREATE_NO_WINDOW);
        }

        match cmd.output() {
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

    fn validate_device_serial(device: &str) -> bool {
        !device.is_empty()
            && device
                .chars()
                .all(|c| c.is_alphanumeric() || c == ':' || c == '-' || c == '_' || c == '.')
    }

    pub fn setup_proxy(&self, device: &str) -> bool {
        if !Self::validate_device_serial(device) {
            log::error!("[adb] invalid device serial: {}", device);
            return false;
        }

        let proxy_port = self.proxy_port.load(Ordering::SeqCst);
        let port_str = format!("127.0.0.1:{}", proxy_port);
        let tcp = format!("tcp:{}", proxy_port);

        let (code, _, err) = Self::run_adb(&[
            "-s", device, "shell", "settings", "put", "global", "http_proxy", &port_str,
        ]);
        if code != 0 {
            log::warn!(
                "[adb] setup_proxy http_proxy failed, code={}, stderr={}",
                code,
                err
            );
            return false;
        }
        if !err.is_empty() {
            log::warn!("[adb] setup_proxy http_proxy stderr: {}", err);
        }

        let (code, _, err) = Self::run_adb(&["-s", device, "reverse", &tcp, &tcp]);
        if code != 0 {
            log::warn!(
                "[adb] setup_proxy reverse failed, code={}, stderr={}",
                code,
                err
            );
            return false;
        }
        if !err.is_empty() {
            log::warn!("[adb] setup_proxy reverse stderr: {}", err);
        }

        true
    }

    pub fn teardown_proxy(&self, device: &str) {
        if !Self::validate_device_serial(device) {
            log::error!("[adb] invalid device serial: {}", device);
            return;
        }

        let proxy_port = self.proxy_port.load(Ordering::SeqCst);
        let tcp = format!("tcp:{}", proxy_port);
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

        let handle = std::thread::spawn(move || {
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
                // Sleep in small increments so stop_polling returns quickly.
                for _ in 0..30 {
                    if !polling.load(Ordering::SeqCst) {
                        break;
                    }
                    std::thread::sleep(Duration::from_millis(100));
                }
            }
        });

        *self.poll_thread.lock() = Some(handle);
    }

    pub fn stop_polling(&self) {
        self.polling.store(false, Ordering::SeqCst);

        if let Some(handle) = self.poll_thread.lock().take() {
            let _ = handle.join();
        }
    }

}
