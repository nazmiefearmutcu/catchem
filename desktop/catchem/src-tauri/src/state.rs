//! Process-wide Catchem state shared with Tauri commands.

use std::sync::{Arc, RwLock};

use crate::sidecar::{SidecarConfig, SidecarState};

pub struct AppState {
    pub sidecar: Arc<SidecarState>,
    pub sidecar_config: RwLock<SidecarConfig>,
}

impl AppState {
    pub fn new(cfg: SidecarConfig) -> Arc<Self> {
        Arc::new(Self {
            sidecar: SidecarState::new(),
            sidecar_config: RwLock::new(cfg),
        })
    }
}
