//! Security primitives the Tauri shell enforces:
//!
//! * Only HTTP/HTTPS external links open in the system browser.
//! * Navigation inside the webview is locked to the local API origin.
//! * Production-safe is the only mode the desktop shell ever sets at spawn.

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
}
