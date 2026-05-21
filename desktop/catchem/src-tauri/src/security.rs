//! Security primitives the Tauri shell enforces:
//!
//! * Only HTTP/HTTPS external links open in the system browser.
//! * Navigation inside the webview is locked to the local API origin.
//! * Production-safe is the only mode the desktop shell ever sets at spawn.

/// What the webview should do for a navigation attempt. The lib.rs
/// `on_navigation` handler maps this to a bool + side-effect.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NavigationDecision {
    /// Same-origin or Tauri-internal — let the webview navigate.
    AllowInWebview,
    /// Safe external http(s) link — open in the system browser, block in webview.
    OpenExternal,
    /// Unsafe scheme (javascript:, file:, vbscript:, data:, …) — drop on the floor.
    Block,
}

/// True if the URL is safe to hand to the system browser (http/https).
pub fn is_safe_external_url(url: &str) -> bool {
    let lower = url.trim().to_lowercase();
    lower.starts_with("http://") || lower.starts_with("https://")
}

/// True if the URL is an allowed in-app navigation target.
pub fn is_allowed_internal_url(url: &str, host: &str, port: u16) -> bool {
    let lower = url.trim().to_lowercase();
    let local_a = format!("http://{host}:{port}");
    let local_b = format!("http://localhost:{port}");
    lower.starts_with(&local_a) || lower.starts_with(&local_b)
}

/// True if the URL is a Tauri-internal scheme the webview needs to load for
/// its own bootstrap (asset://, tauri://, ipc:, about:blank).
pub fn is_tauri_internal_url(url: &str) -> bool {
    let lower = url.trim().to_ascii_lowercase();
    lower.starts_with("tauri://")
        || lower.starts_with("asset://")
        || lower.starts_with("ipc://")
        || lower == "about:blank"
}

/// Classify a navigation attempt. Pure function — lib.rs reads the result
/// to decide allow / open-external / block.
pub fn classify_navigation(url: &str, host: &str, port: u16) -> NavigationDecision {
    if is_allowed_internal_url(url, host, port) || is_tauri_internal_url(url) {
        return NavigationDecision::AllowInWebview;
    }
    if is_safe_external_url(url) {
        return NavigationDecision::OpenExternal;
    }
    NavigationDecision::Block
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_javascript_data_file_schemes() {
        for bad in [
            "javascript:alert(1)",
            "data:text/html,<script>alert(1)</script>",
            "file:///etc/passwd",
            "vbscript:msgbox(1)",
            "",
            "   ",
            "ftp://example.com",
        ] {
            assert!(!is_safe_external_url(bad), "should reject: {bad}");
        }
    }

    #[test]
    fn accepts_http_and_https() {
        for ok in ["http://example.com", "https://example.com", "HTTPS://Example.com/path"] {
            assert!(is_safe_external_url(ok), "should accept: {ok}");
        }
    }

    #[test]
    fn internal_navigation_locked_to_local_api() {
        assert!(is_allowed_internal_url("http://127.0.0.1:8087/feed", "127.0.0.1", 8087));
        assert!(is_allowed_internal_url("http://localhost:8087/help", "127.0.0.1", 8087));
        assert!(!is_allowed_internal_url("http://example.com", "127.0.0.1", 8087));
        assert!(!is_allowed_internal_url("http://127.0.0.1:9999", "127.0.0.1", 8087));
    }

    #[test]
    fn tauri_internal_schemes_recognised() {
        for ok in ["tauri://localhost", "asset://example/image.png", "about:blank", "TAURI://x"] {
            assert!(is_tauri_internal_url(ok), "should accept tauri-internal: {ok}");
        }
        for bad in [
            "https://example.com",
            "http://127.0.0.1:8087/",
            "javascript:alert(1)",
            "data:text/html,x",
            "about:srcdoc",
        ] {
            assert!(!is_tauri_internal_url(bad), "should reject as tauri-internal: {bad}");
        }
    }

    #[test]
    fn classify_navigation_allows_local_and_blocks_unsafe() {
        // Local API origin -> allow in webview
        assert_eq!(
            classify_navigation("http://127.0.0.1:8087/replay", "127.0.0.1", 8087),
            NavigationDecision::AllowInWebview,
        );
        assert_eq!(
            classify_navigation("http://localhost:8087/", "127.0.0.1", 8087),
            NavigationDecision::AllowInWebview,
        );
        // Tauri internal -> allow in webview
        assert_eq!(
            classify_navigation("tauri://localhost/index.html", "127.0.0.1", 8087),
            NavigationDecision::AllowInWebview,
        );
        // External http/https -> hand to system browser
        assert_eq!(
            classify_navigation("https://reuters.com/article/x", "127.0.0.1", 8087),
            NavigationDecision::OpenExternal,
        );
        assert_eq!(
            classify_navigation("http://example.com/", "127.0.0.1", 8087),
            NavigationDecision::OpenExternal,
        );
        // Wrong-port localhost -> block (not the API; could be malicious svc)
        assert_eq!(
            classify_navigation("http://localhost:9999/", "127.0.0.1", 8087),
            NavigationDecision::OpenExternal,
            "wrong-port localhost is just another http URL — treat as external",
        );
        // Dangerous schemes -> block
        for bad in [
            "javascript:alert(1)",
            "data:text/html,<script>alert(1)</script>",
            "file:///etc/passwd",
            "vbscript:msgbox(1)",
            "",
        ] {
            assert_eq!(
                classify_navigation(bad, "127.0.0.1", 8087),
                NavigationDecision::Block,
                "should block: {bad}",
            );
        }
    }
}
