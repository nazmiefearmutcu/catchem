const BOOT_TOKEN_STORAGE_KEY = "catchem.boot_token";
const BOOT_TOKEN_WINDOW_NAME_PREFIX = "catchem.boot_token=";
let cachedBootToken = "";

declare global {
  interface Window {
    __CATCHEM_BOOT_TOKEN__?: string;
  }
}

function readTokenFromBootstrapGlobal(): string {
  if (typeof window === "undefined") return "";
  return window.__CATCHEM_BOOT_TOKEN__?.trim() || "";
}

function safeSessionStorageGet(): string {
  if (typeof sessionStorage === "undefined") return "";
  try {
    return sessionStorage.getItem(BOOT_TOKEN_STORAGE_KEY) ?? "";
  } catch {
    return "";
  }
}

function safeWindowNameSet(token: string): void {
  if (!token || typeof window === "undefined") return;
  try {
    window.name = `${BOOT_TOKEN_WINDOW_NAME_PREFIX}${token}`;
  } catch {
    // window.name is best-effort only. When it is blocked, the boot token
    // still survives in-memory for the current page and in sessionStorage
    // when the browser permits it.
  }
}

function rememberBootToken(token: string): string {
  const normalized = token.trim();
  if (!normalized) return "";
  cachedBootToken = normalized;
  safeSessionStorageSet(normalized);
  safeWindowNameSet(normalized);
  return normalized;
}

function safeSessionStorageSet(token: string): void {
  if (!token || typeof sessionStorage === "undefined") return;
  try {
    sessionStorage.setItem(BOOT_TOKEN_STORAGE_KEY, token);
  } catch {
    // Storage is best-effort only. If the platform denies it, callers still
    // fall back to the raw query string when available.
  }
}

function readTokenFromWindowName(): string {
  if (typeof window === "undefined") return "";
  const name = window.name || "";
  if (!name.startsWith(BOOT_TOKEN_WINDOW_NAME_PREFIX)) return "";
  return name.slice(BOOT_TOKEN_WINDOW_NAME_PREFIX.length);
}

export function persistBootToken(token: string): void {
  rememberBootToken(token);
}

export function getBootToken(): string {
  if (cachedBootToken) return cachedBootToken;

  const bootstrapToken = readTokenFromBootstrapGlobal();
  if (bootstrapToken) return rememberBootToken(bootstrapToken);

  const currentUrlToken =
    typeof window === "undefined"
      ? ""
      : (() => {
          try {
            return new URL(window.location.href).searchParams.get("boot_token") || "";
          } catch {
            return "";
          }
        })();
  if (currentUrlToken) return rememberBootToken(currentUrlToken);

  const windowNameToken = readTokenFromWindowName();
  if (windowNameToken) return rememberBootToken(windowNameToken);

  const stored = safeSessionStorageGet();
  if (stored) return rememberBootToken(stored);

  return "";
}

export function resetBootTokenCacheForTests(): void {
  cachedBootToken = "";
}

export { BOOT_TOKEN_STORAGE_KEY };
