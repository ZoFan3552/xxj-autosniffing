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

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Config {
    #[serde(default)]
    pub encrypt_url: String,
    #[serde(default)]
    pub decrypt_url: String,
    #[serde(default = "default_proxy_port")]
    pub proxy_port: u16,
    #[serde(default = "default_capture_hosts")]
    pub capture_hosts: Vec<String>,
    #[serde(default)]
    pub breakpoints: Vec<BreakpointRule>,
}

fn default_proxy_port() -> u16 { 8080 }
fn default_true() -> bool { true }
fn default_capture_hosts() -> Vec<String> {
    vec!["xunfeixxj.com".to_string()]
}

impl Default for Config {
    fn default() -> Self {
        Self {
            encrypt_url: String::new(),
            decrypt_url: String::new(),
            proxy_port: 8080,
            capture_hosts: default_capture_hosts(),
            breakpoints: Vec::new(),
        }
    }
}

impl Config {
    pub fn load() -> Self {
        let path = config_path();
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
