use serde::{Deserialize, Serialize};
use std::collections::HashMap;

#[derive(Debug, Clone, Serialize, Deserialize)]
#[allow(dead_code)]
pub struct RequestRecord {
    pub flow_id: String,
    pub method: String,
    pub url: String,
    pub request_headers: HashMap<String, String>,
    pub request_plain: Option<String>,
    pub response_status: Option<u16>,
    pub response_headers: Option<HashMap<String, String>>,
    pub response_plain: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[allow(dead_code)]
pub struct InterceptRequest {
    pub flow_id: String,
    pub method: String,
    pub url: String,
    pub request_plain: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum InterceptAction {
    Pass,
    Abort,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InterceptResponse {
    pub flow_id: String,
    pub action: InterceptAction,
    pub body: Option<String>,
}
