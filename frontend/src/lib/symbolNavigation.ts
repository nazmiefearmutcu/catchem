export const SYMBOL_NAV_PATTERN = /^[A-Za-z0-9.\-]{1,12}$/;

/**
 * Normalize user-entered symbol-ish query text for local route lookups.
 *
 * - Trims surrounding whitespace.
 * - Strips a single leading "$" prefix used in many financial tickers.
 * - Upper-cases for route consistency.
 */
export function normalizeSymbolQuery(raw: string): string {
  return raw.trim().replace(/^\$/, "").toUpperCase();
}

/**
 * Return a URL-safe, canonical symbol for `/symbols/:symbol` navigation.
 * Returns null when the input cannot be interpreted as a valid symbol-like
 * token (e.g. empty, too long, contains spaces or punctuation).
 */
export function parseSymbolNavigation(raw: string): string | null {
  const normalized = normalizeSymbolQuery(raw);
  if (!SYMBOL_NAV_PATTERN.test(normalized)) return null;
  return normalized;
}

/**
 * Resolve `/symbols/:symbol` route for local navigations.
 *
 * Returns a fully encoded path when `raw` is a valid symbol token,
 * otherwise `null` so callers can keep the current surface open
 * instead of dispatching a best-effort (and possibly broken) route.
 */
export function buildSymbolRoute(raw: string): string | null {
  const symbol = parseSymbolNavigation(raw);
  if (!symbol) return null;
  return `/symbols/${encodeURIComponent(symbol)}`;
}
