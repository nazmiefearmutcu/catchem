//! Sidecar manager.
//!
//! Spawns the catchem FastAPI server as a child process, polls
//! /healthz until it answers, and exposes start/stop/status to the
//! Tauri command layer.

use serde::{Deserialize, Serialize};
use std::fs::OpenOptions;
use std::io::Write;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use crate::paths;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SidecarConfig {
    /// Absolute path to a Python interpreter (`.venv/bin/python` in dev,
    /// the bundled PyInstaller binary in release).
    pub python: PathBuf,
    /// Working directory the process should launch from (repo root in dev).
    pub cwd: PathBuf,
    /// API host + port the server listens on.
    pub host: String,
    pub port: u16,
    /// `true` for the bundled .app build — sidecar must write all output
    /// under `~/Library/Application Support/Catchem/`, NOT relative to
    /// the (read-only, codesigned) bundle. Dev builds keep the repo-local
    /// `data/` layout so analysts can `git diff` outputs.
    #[serde(default)]
    pub release_mode: bool,
}

impl SidecarConfig {
    pub fn endpoint(&self) -> String {
        format!("http://{}:{}", self.host, self.port)
    }
}

#[derive(Default)]
pub struct SidecarState {
    inner: Mutex<Option<Child>>,
}

impl SidecarState {
    pub fn new() -> Arc<Self> {
        Arc::new(Self::default())
    }

    /// Spawn the sidecar. If one is already running and `force` is false,
    /// returns Ok(()) without touching it.
    pub fn start(&self, cfg: &SidecarConfig, force: bool) -> Result<(), String> {
        let mut guard = self.inner.lock().map_err(|e| e.to_string())?;
        if guard.is_some() && !force {
            return Ok(());
        }
        if let Some(mut child) = guard.take() {
            // best-effort kill if we were forcing restart
            let _ = child.kill();
            let _ = child.wait();
        }
        let mut cmd = Command::new(&cfg.python);
        cmd.arg("-m").arg("catchem.cli").arg("serve");
        cmd.current_dir(&cfg.cwd);
        // Production-safe is the only acceptable default. Diagnostic must
        // never be enabled from the desktop shell.
        cmd.env("CATCHEM_MODE", "production_safe");
        cmd.env("CATCHEM_GUARDS__NEWSIMPACT_DIAGNOSTIC_ENABLED", "false");
        // The Python settings layer keys host/port under `api.host` and
        // `api.port`, so the env override has to use pydantic-settings'
        // nested delimiter (`__`). Without the double underscore the
        // sidecar would silently ignore us and bind whatever
        // `configs/catchem.yaml` says (default 8087), which in turn
        // breaks the Tauri shell's polling URL the moment anyone tries
        // to move the port (e.g. avoid a conflict with another local
        // service or run a second sidecar for testing).
        cmd.env("CATCHEM_API__HOST", &cfg.host);
        cmd.env("CATCHEM_API__PORT", cfg.port.to_string());
        // Stub default — the user can opt into HF via a separate setup script.
        cmd.env("CATCHEM_USE_ML_STUBS", "true");
        // Force unbuffered Python output so errors surface immediately.
        cmd.env("PYTHONUNBUFFERED", "1");
        // Release-only: pin the writable data root to Application Support.
        // Dev builds intentionally inherit the repo-local `data/` layout
        // so analysts can `git diff` outputs.
        if cfg.release_mode {
            let out_dir = paths::release_catchem_output_dir();
            let aw_dir = paths::release_awareness_data_dir();
            cmd.env("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", &out_dir);
            cmd.env("CATCHEM_PATHS__AWARENESS_DATA_DIR", &aw_dir);
            log::info!(
                "sidecar release-mode paths: out={} awareness_data={}",
                out_dir.display(),
                aw_dir.display()
            );
            // Optional persistent reviewer config — drop a key=value file
            // at `~/Library/Application Support/Catchem/reviewers.env` and
            // every launch reads it. Lets the operator set the DeepSeek
            // API key once instead of re-pasting it via Settings after
            // every relaunch. We INTENTIONALLY only forward CATCHEM_*
            // variables so the file can't smuggle in arbitrary process
            // env (e.g., PATH, HOME).
            if let Some(extra) = load_persistent_env_file() {
                for (k, v) in extra {
                    if k.starts_with("CATCHEM_") {
                        cmd.env(&k, &v);
                    }
                }
            }
        }

        // Redirect stdout+stderr to a log file we can tail. Piped+undrained
        // streams will deadlock the child once the pipe buffer fills, which
        // silently breaks "sidecar isn't binding" debugging.
        let log_path = paths::sidecar_log_path();
        let log_file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&log_path)
            .map_err(|e| format!("open sidecar log {}: {e}", log_path.display()))?;
        let log_file_err = log_file
            .try_clone()
            .map_err(|e| format!("clone sidecar log fd: {e}"))?;
        // Marker so successive launches are easy to separate in the file.
        {
            let mut banner = log_file
                .try_clone()
                .map_err(|e| format!("clone sidecar log fd: {e}"))?;
            let _ = writeln!(
                banner,
                "\n=== catchem sidecar start {} python={} cwd={} ===",
                chrono_like_now(),
                cfg.python.display(),
                cfg.cwd.display()
            );
        }
        cmd.stdout(Stdio::from(log_file));
        cmd.stderr(Stdio::from(log_file_err));

        let child = match cmd.spawn() {
            Ok(c) => c,
            Err(e) => {
                // Surface the spawn failure into the same sidecar.log file so
                // the user (and we, during debug) can see *why* the child
                // never came up. env_logger writes to stderr which launchd
                // discards for `open`-launched bundles.
                if let Ok(mut f) = OpenOptions::new().create(true).append(true).open(&log_path) {
                    let _ = writeln!(
                        f,
                        "[SPAWN FAILURE] failed to spawn {} cwd={}: {e}",
                        cfg.python.display(),
                        cfg.cwd.display()
                    );
                }
                return Err(format!("failed to spawn sidecar: {e}"));
            }
        };
        log::info!(
            "sidecar spawned pid={} log={}",
            child.id(),
            log_path.display()
        );
        *guard = Some(child);
        Ok(())
    }

    pub fn stop(&self) -> Result<(), String> {
        let mut guard = self.inner.lock().map_err(|e| e.to_string())?;
        if let Some(mut child) = guard.take() {
            log::info!("sidecar stop requested pid={}", child.id());
            let _ = child.kill();
            let _ = child.wait();
        }
        Ok(())
    }

    pub fn restart(&self, cfg: &SidecarConfig) -> Result<(), String> {
        self.stop()?;
        self.start(cfg, true)
    }

    pub fn pid(&self) -> Option<u32> {
        self.inner.lock().ok().and_then(|g| g.as_ref().map(|c| c.id()))
    }
}

#[derive(Debug, Serialize)]
pub struct WaitForHealthOutcome {
    pub healthy: bool,
    pub elapsed_ms: u128,
    pub last_status: Option<u16>,
    pub last_error: Option<String>,
}

/// ISO-ish timestamp using only stdlib — avoids pulling chrono just for a
/// log banner. Format: `1970-01-01T00:00:00Z` style at second resolution.
/// Read `~/Library/Application Support/Catchem/reviewers.env` and parse
/// it as a list of `KEY=VALUE` pairs. Missing file is a normal "nothing
/// to inject" state — returns None silently. Lines beginning with `#` or
/// blank lines are skipped. Values are NOT shell-evaluated; the file is
/// read as plain `KEY=VALUE` text so a leading `$` stays literal.
///
/// Security: refuses to load the file if its POSIX mode includes any
/// group- or world-write bit (mask `0o022`). The file feeds env vars
/// (DeepSeek API keys, etc.) into the sidecar process; if any other
/// local user can write to it they can inject arbitrary CATCHEM_*
/// settings at launch. Owner-write only.
fn load_persistent_env_file() -> Option<Vec<(String, String)>> {
    let path = paths::release_reviewers_env_path();
    load_env_file_from_path(&path)
}

/// Path-parameterised variant of [`load_persistent_env_file`] used for
/// unit testing the world-writable guard. Behaves identically to the
/// public function: missing file → None, group/world-writable file →
/// None plus a warn-level log, otherwise parses KEY=VALUE lines.
fn load_env_file_from_path(path: &std::path::Path) -> Option<Vec<(String, String)>> {
    // Check permissions BEFORE reading. On macOS/Unix, refuse to load if
    // group or world has write access — that means another user on the
    // box can inject env vars into our sidecar.
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if let Ok(meta) = std::fs::metadata(path) {
            let mode = meta.permissions().mode();
            if mode & 0o022 != 0 {
                log::warn!(
                    "refusing to load reviewers.env: mode {:o} grants group/world write (path={}); fix with `chmod 600`",
                    mode & 0o777,
                    path.display()
                );
                return None;
            }
        }
        // Missing-file case falls through to the read_to_string below,
        // which returns the same "nothing to inject" None path.
    }
    let text = match std::fs::read_to_string(path) {
        Ok(s) => s,
        Err(_) => return None,
    };
    let mut out: Vec<(String, String)> = Vec::new();
    for raw in text.lines() {
        let line = raw.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        if let Some((k, v)) = line.split_once('=') {
            // Strip optional surrounding double or single quotes so the
            // operator can write API_KEY="sk-..." OR API_KEY=sk-... and
            // both work identically.
            let key = k.trim().to_string();
            let mut val = v.trim().to_string();
            if (val.starts_with('"') && val.ends_with('"'))
                || (val.starts_with('\'') && val.ends_with('\''))
            {
                if val.len() >= 2 {
                    val = val[1..val.len() - 1].to_string();
                }
            }
            if !key.is_empty() {
                out.push((key, val));
            }
        }
    }
    if out.is_empty() {
        None
    } else {
        log::info!(
            "loaded {} persistent reviewer env entries from {}",
            out.len(),
            path.display()
        );
        Some(out)
    }
}

fn chrono_like_now() -> String {
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    // Quick Y/M/D/H/M/S split using the standard "days since epoch" trick.
    let days = (secs / 86_400) as i64;
    let rem = secs % 86_400;
    let h = rem / 3600;
    let m = (rem % 3600) / 60;
    let s = rem % 60;
    let (y, mo, d) = days_to_ymd(days);
    format!("{:04}-{:02}-{:02}T{:02}:{:02}:{:02}Z", y, mo, d, h, m, s)
}

/// Civil-date conversion from days since 1970-01-01. Algorithm by Howard
/// Hinnant (date.h), public domain. Avoids chrono.
fn days_to_ymd(z: i64) -> (i64, u32, u32) {
    let z = z + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097) as u32;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = if m <= 2 { y + 1 } else { y };
    (y, m, d)
}

/// Poll `/healthz` until 200 or timeout.
pub async fn wait_for_health(cfg: &SidecarConfig, timeout: Duration) -> WaitForHealthOutcome {
    let url = format!("{}/healthz", cfg.endpoint());
    let started = Instant::now();
    let mut last_status: Option<u16> = None;
    let mut last_error: Option<String> = None;
    let client = reqwest::Client::builder()
        .timeout(Duration::from_millis(800))
        .build()
        .expect("reqwest client");

    while started.elapsed() < timeout {
        match client.get(&url).send().await {
            Ok(resp) => {
                last_status = Some(resp.status().as_u16());
                if resp.status().is_success() {
                    return WaitForHealthOutcome {
                        healthy: true,
                        elapsed_ms: started.elapsed().as_millis(),
                        last_status,
                        last_error: None,
                    };
                }
            }
            Err(e) => {
                last_error = Some(e.to_string());
            }
        }
        tokio::time::sleep(Duration::from_millis(250)).await;
    }
    WaitForHealthOutcome {
        healthy: false,
        elapsed_ms: started.elapsed().as_millis(),
        last_status,
        last_error,
    }
}

#[cfg(test)]
#[cfg(unix)]
mod tests {
    use super::load_env_file_from_path;
    use std::io::Write;
    use std::os::unix::fs::PermissionsExt;

    fn write_tmp(name: &str, body: &str) -> std::path::PathBuf {
        let mut p = std::env::temp_dir();
        let nonce = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        p.push(format!("catchem-test-{}-{}-{}", name, std::process::id(), nonce));
        let mut f = std::fs::File::create(&p).expect("create tmp");
        f.write_all(body.as_bytes()).expect("write tmp");
        p
    }

    #[test]
    fn loads_keys_when_mode_is_owner_only() {
        let p = write_tmp("ok", "CATCHEM_FOO=bar\nCATCHEM_BAZ=\"q\"\n# comment\n\n");
        std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o600)).unwrap();
        let out = load_env_file_from_path(&p).expect("should parse");
        assert_eq!(out.len(), 2);
        assert_eq!(out[0], ("CATCHEM_FOO".to_string(), "bar".to_string()));
        assert_eq!(out[1], ("CATCHEM_BAZ".to_string(), "q".to_string()));
        let _ = std::fs::remove_file(&p);
    }

    #[test]
    fn refuses_group_writable_file() {
        let p = write_tmp("groupw", "CATCHEM_FOO=bar\n");
        // 0o620 = owner rw, group w, world none — group-write bit set.
        std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o620)).unwrap();
        assert!(load_env_file_from_path(&p).is_none(),
            "group-writable env file must be skipped");
        let _ = std::fs::remove_file(&p);
    }

    #[test]
    fn refuses_world_writable_file() {
        let p = write_tmp("worldw", "CATCHEM_FOO=bar\n");
        // 0o602 = owner rw, group none, world w — world-write bit set.
        std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o602)).unwrap();
        assert!(load_env_file_from_path(&p).is_none(),
            "world-writable env file must be skipped");
        let _ = std::fs::remove_file(&p);
    }

    #[test]
    fn missing_file_returns_none() {
        let mut p = std::env::temp_dir();
        p.push("catchem-test-missing-does-not-exist-xyz");
        assert!(load_env_file_from_path(&p).is_none());
    }
}
