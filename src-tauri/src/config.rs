use serde::{Deserialize, Serialize};
use std::fs;
use std::path::PathBuf;
use std::sync::OnceLock;

static CONFIG_PATH: OnceLock<PathBuf> = OnceLock::new();

fn config_path() -> &'static PathBuf {
    CONFIG_PATH.get_or_init(|| {
        if let Some(d) = dirs::config_dir() {
            let app_dir = d.join("xxj-auto-sniffing");
            let _ = fs::create_dir_all(&app_dir);
            return app_dir.join("config.json");
        }
        // Fallback: place config.json next to the executable.
        // current_exe() can fail or return an empty path, so guard against that.
        match std::env::current_exe() {
            Ok(mut p) if !p.as_os_str().is_empty() => {
                p.pop();
                p.push("config.json");
                p
            }
            _ => PathBuf::from("config.json"),
        }
    })
}

/// A single Charles-style breakpoint rule.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BreakpointRule {
    pub url_pattern: String,
    #[serde(default = "default_true")]
    pub break_request: bool,
    #[serde(default)]
    pub break_response: bool,
    #[serde(default = "default_true")]
    pub enabled: bool,
}

/// A single mock rule: a URL match plus what to answer with.
///
/// `mode` is either `"respond"` (answer without contacting the server) or `"patch"`
/// (forward the request, then merge `body` into the real JSON response).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MockRule {
    #[serde(default)]
    pub id: String,
    #[serde(default)]
    pub name: String,
    pub url_pattern: String,
    /// Empty means any method.
    #[serde(default)]
    pub method: String,
    #[serde(default = "default_mock_mode")]
    pub mode: String,
    /// Response status for `respond` mode.
    #[serde(default = "default_mock_status")]
    pub status: u16,
    /// Plain response body for `respond` mode, or the JSON fragment for `patch` mode.
    #[serde(default)]
    pub body: String,
    /// Hit cap; 0 means unlimited.
    #[serde(default)]
    pub times: u32,
    #[serde(default)]
    pub delay_ms: u32,
    /// Whether the body is encrypted before being sent back.
    #[serde(default = "default_true")]
    pub encrypt: bool,
    #[serde(default = "default_true")]
    pub enabled: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Config {
    #[serde(default)]
    pub encrypt_url: String,
    #[serde(default)]
    pub decrypt_url: String,
    /// Raw secret for in-process crypto. Non-empty takes priority over the URLs above.
    #[serde(default)]
    pub crypto_secret: String,
    /// Whether `crypto_secret` is base64-encoded rather than plain text.
    #[serde(default)]
    pub crypto_secret_b64: bool,
    #[serde(default = "default_proxy_port")]
    pub proxy_port: u16,
    #[serde(default = "default_capture_hosts")]
    pub capture_hosts: Vec<String>,
    #[serde(default)]
    pub breakpoints: Vec<BreakpointRule>,
    #[serde(default)]
    pub mock_rules: Vec<MockRule>,
}

fn default_proxy_port() -> u16 { 8080 }
fn default_true() -> bool { true }
fn default_mock_mode() -> String { "respond".to_string() }
fn default_mock_status() -> u16 { 200 }
fn default_capture_hosts() -> Vec<String> {
    vec!["xunfeixxj.com".to_string()]
}

impl Default for Config {
    fn default() -> Self {
        Self {
            encrypt_url: String::new(),
            decrypt_url: String::new(),
            crypto_secret: String::new(),
            crypto_secret_b64: false,
            proxy_port: 8080,
            capture_hosts: default_capture_hosts(),
            breakpoints: Vec::new(),
            mock_rules: Vec::new(),
        }
    }
}

impl Config {
    pub fn load() -> Self {
        let path = config_path();
        log::info!("[config] config path: {}", path.display());
        match fs::read_to_string(path) {
            Ok(content) => serde_json::from_str(&content).unwrap_or_default(),
            Err(_) => {
                let cfg = Self::default();
                cfg.save().ok();
                cfg
            }
        }
    }

    pub fn save(&self) -> Result<(), String> {
        let path = config_path();
        let json = serde_json::to_string_pretty(self).map_err(|e| e.to_string())?;
        fs::write(path, json).map_err(|e| e.to_string())
    }

    /// The `update_config` command pushed to the Python subprocess over stdin.
    ///
    /// Sensitive fields (the crypto secret and endpoint URLs) travel only on this
    /// channel, never as command-line arguments where other processes could read them.
    pub fn update_command(&self) -> serde_json::Value {
        serde_json::json!({
            "command": "update_config",
            "encrypt_url": self.encrypt_url,
            "decrypt_url": self.decrypt_url,
            "crypto_secret": self.crypto_secret,
            "crypto_secret_b64": self.crypto_secret_b64,
            "capture_hosts": self.capture_hosts,
            "breakpoints": self.breakpoints,
            "mock_rules": self.mock_rules,
        })
    }

    pub fn merge_from(&mut self, data: &serde_json::Value) -> Result<(), String> {
        // Validate proxy_port range before applying any changes.
        if let Some(v) = data.get("proxy_port").and_then(|v| v.as_u64()) {
            if v == 0 || v > u16::MAX as u64 {
                return Err("proxy_port must be in range 1..=65535".to_string());
            }
        }

        // Serialize current config → JSON object, patch with incoming fields,
        // then deserialize back. New config fields are automatically handled.
        let mut merged =
            serde_json::to_value(&*self).map_err(|e| format!("config serialize error: {e}"))?;
        if let (Some(base), Some(patch)) = (merged.as_object_mut(), data.as_object()) {
            for (k, v) in patch {
                base.insert(k.clone(), v.clone());
            }
        }
        *self =
            serde_json::from_value(merged).map_err(|e| format!("config deserialize error: {e}"))?;
        Ok(())
    }
}
