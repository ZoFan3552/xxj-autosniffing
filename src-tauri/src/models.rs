use serde::{Deserialize, Serialize};

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
