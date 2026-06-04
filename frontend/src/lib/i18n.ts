import { useSyncExternalStore } from "react";

/**
 * Minimal, dependency-free i18n layer for the cockpit.
 *
 * Catchem ships with two locales for v31:
 *   - "en" (default — every string in the system)
 *   - "tr" (Turkish, partial — high-traffic strings only)
 *
 * Usage:
 *   import { t, useLang, setLang } from "@/lib/i18n";
 *   t("nav.overview")     → "Overview" / "Genel Bakış"
 *   useLang()             → re-renders the component on locale change
 *   setLang("tr")         → persists + broadcasts to every subscriber
 *
 * Persistence:
 *   - `catchem.lang` → "en" | "tr" (localStorage)
 *
 * Fall-through:
 *   - Missing key in current locale → fall back to English
 *   - Missing key in both → return the key itself (so the string is at
 *     least debuggable in the DOM rather than rendering "undefined").
 *
 * The store implements the `useSyncExternalStore` contract so React 18
 * concurrent rendering tears correctly across the tree; no Context
 * provider is required, which means non-component code (e.g.
 * snapshot.ts, api.ts error messages) can also call t() at module
 * import time.
 *
 * No new dependencies — we intentionally avoid react-i18next / formatjs
 * here. v31's scope is "translate the surface the user sees most",
 * not "full ICU MessageFormat with plural rules".
 */

export type Lang = "en" | "tr";

export const I18N_KEY = "catchem.lang";

const SUPPORTED: ReadonlySet<Lang> = new Set<Lang>(["en", "tr"]);

// String dictionary. Add a key here, then call `t("key")` in components.
// Categories: nav.*, common.*, settings.*, feed.*, overview.*, ops.*,
// benchmark.*, shortcuts.*. Keep keys short, namespaced, lowercase.
const STRINGS: Record<Lang, Record<string, string>> = {
  en: {
    // ── Top-level nav (Shell.tsx) ──────────────────────────────────────
    "nav.overview": "Overview",
    "nav.feed": "Live Feed",
    "nav.replay": "Replay/Upload",
    "nav.analysis": "Analysis",
    "nav.symbols": "Symbols",
    "nav.portfolio": "Portfolio",
    "nav.tags": "Tags",
    "nav.benchmark": "Benchmark",
    "nav.backtest": "Backtest",
    "nav.reviews": "Reviews",
    "nav.scan": "Quant Scan",
    "nav.logs": "Logs",
    "nav.sources": "Sources",
    "nav.model_controls": "Model Controls",
    "nav.ops": "Ops",
    "nav.settings": "Settings",
    "nav.help": "Help",
    // ── Common verbs / nouns reused across pages ───────────────────────
    "common.search": "Search",
    "common.filter": "Filter",
    "common.export": "Export",
    "common.import": "Import",
    "common.reload": "Reload",
    "common.cancel": "Cancel",
    "common.save": "Save",
    "common.dismiss": "Dismiss",
    "common.close": "Close",
    "common.confirm": "Confirm",
    "common.error": "Error",
    "common.loading": "Loading…",
    "common.no_data": "No data yet",
    // ── Settings page ──────────────────────────────────────────────────
    "settings.theme": "Theme",
    "settings.accent": "Accent color",
    "settings.language": "Language",
    "settings.language.en": "English",
    "settings.language.tr": "Türkçe",
    "settings.language.hint":
      "UI language for nav, common buttons, and high-traffic copy.",
    "settings.database": "Database",
    "settings.shortcuts": "Keyboard shortcuts",
    // ── Feed page (FeedPage.tsx) ───────────────────────────────────────
    "feed.search.placeholder": "title, domain, symbol…",
    "feed.relevance": "relevance",
    "feed.relevance.only": "finance-only",
    "feed.relevance.all": "all",
    "feed.records": "records",
    "feed.refreshing": "refreshing…",
    // ── Overview / Welcome (OverviewPage.tsx) ─────────────────────────
    "overview.eyebrow": "Catchem · analyst workstation",
    // ── Ops page ───────────────────────────────────────────────────────
    "ops.title.nominal": "All systems nominal",
    "ops.db_breakdown.title": "Database breakdown",
    "ops.db_breakdown.summary": "{tables} tables · {indexes} indexes · {sizeMb} MB",
    // ── Benchmark page ─────────────────────────────────────────────────
    "benchmark.eyebrow": "Benchmark Lab · golden-set evaluation",
    // ── Backtest page ──────────────────────────────────────────────────
    "backtest.eyebrow": "Backtest · prediction calibration",
    // ── Keyboard shortcut overlay (?) ──────────────────────────────────
    "shortcuts.title": "Every chord, every action",
    "shortcuts.section.nav": "Navigation",
    "shortcuts.section.actions": "Actions",
    "shortcuts.section.modals": "Modals",
    // ── Quant scan signals (PersistencePanel, ...) ────────────────────
    "quant.persistence.title": "persistence · structural narratives",
    "quant.persistence.summary": "window {days}d · {scopes} scopes",
    // ── Quant fail-soft observability (DegradedSignalsPill, v74) ───────
    "quant.degraded.label": "{n} signals degraded",
  },
  tr: {
    // ── Top-level nav (Shell.tsx) ──────────────────────────────────────
    "nav.overview": "Genel Bakış",
    "nav.feed": "Canlı Akış",
    "nav.replay": "Tekrar/Yükle",
    "nav.analysis": "Analiz",
    "nav.symbols": "Semboller",
    "nav.portfolio": "Portföy",
    "nav.tags": "Etiketler",
    "nav.benchmark": "Benchmark",
    "nav.backtest": "Geri Test",
    "nav.reviews": "İncelemeler",
    "nav.scan": "Quant Tarama",
    "nav.logs": "Loglar",
    "nav.sources": "Kaynaklar",
    "nav.model_controls": "Model Kontrolleri",
    "nav.ops": "Sistem",
    "nav.settings": "Ayarlar",
    "nav.help": "Yardım",
    // ── Common verbs / nouns reused across pages ───────────────────────
    "common.search": "Ara",
    "common.filter": "Filtre",
    "common.export": "Dışa Aktar",
    "common.import": "İçe Aktar",
    "common.reload": "Yenile",
    "common.cancel": "İptal",
    "common.save": "Kaydet",
    "common.dismiss": "Kapat",
    "common.close": "Kapat",
    "common.confirm": "Onayla",
    "common.error": "Hata",
    "common.loading": "Yükleniyor…",
    "common.no_data": "Henüz veri yok",
    // ── Settings page ──────────────────────────────────────────────────
    "settings.theme": "Tema",
    "settings.accent": "Vurgu rengi",
    "settings.language": "Dil",
    "settings.language.en": "English",
    "settings.language.tr": "Türkçe",
    "settings.language.hint":
      "Gezinme, ortak butonlar ve sık görünen metinler için arayüz dili.",
    "settings.database": "Veritabanı",
    "settings.shortcuts": "Klavye kısayolları",
    // ── Feed page (FeedPage.tsx) ───────────────────────────────────────
    "feed.search.placeholder": "başlık, alan adı, sembol…",
    "feed.relevance": "alaka",
    "feed.relevance.only": "yalnız finans",
    "feed.relevance.all": "tümü",
    "feed.records": "kayıt",
    "feed.refreshing": "yenileniyor…",
    // ── Overview / Welcome (OverviewPage.tsx) ─────────────────────────
    "overview.eyebrow": "Catchem · analist çalışma istasyonu",
    // ── Ops page ───────────────────────────────────────────────────────
    "ops.title.nominal": "Tüm sistemler normal",
    "ops.db_breakdown.title": "Veritabanı dökümü",
    "ops.db_breakdown.summary": "{tables} tablo · {indexes} indeks · {sizeMb} MB",
    // ── Benchmark page ─────────────────────────────────────────────────
    "benchmark.eyebrow":
      "Benchmark Laboratuvarı · altın küme değerlendirmesi",
    // ── Backtest page ──────────────────────────────────────────────────
    "backtest.eyebrow": "Geri Test · tahmin kalibrasyonu",
    // ── Keyboard shortcut overlay (?) ──────────────────────────────────
    "shortcuts.title": "Her kısayol, her eylem",
    "shortcuts.section.nav": "Gezinme",
    "shortcuts.section.actions": "Eylemler",
    "shortcuts.section.modals": "Modal'lar",
    // ── Quant scan signals (PersistencePanel, ...) ────────────────────
    "quant.persistence.title": "kalıcılık · yapısal anlatılar",
    "quant.persistence.summary": "pencere {days}gün · {scopes} kapsam",
    // ── Quant fail-soft observability (DegradedSignalsPill, v74) ───────
    "quant.degraded.label": "{n} sinyal degrade",
  },
};

// ── Store ─────────────────────────────────────────────────────────────

function readInitialLang(): Lang {
  try {
    if (typeof localStorage === "undefined") return "en";
    const raw = localStorage.getItem(I18N_KEY);
    if (raw && SUPPORTED.has(raw as Lang)) return raw as Lang;
  } catch {
    /* SSR / blocked storage / no DOM — fall through to English */
  }
  return "en";
}

let currentLang: Lang = readInitialLang();
const subscribers = new Set<() => void>();

function notify(): void {
  subscribers.forEach((fn) => {
    try {
      fn();
    } catch {
      /* a subscriber threw — don't take down the rest */
    }
  });
}

function subscribe(fn: () => void): () => void {
  subscribers.add(fn);
  return () => {
    subscribers.delete(fn);
  };
}

// ── Public API ────────────────────────────────────────────────────────

/** Read the current locale (synchronous, no React required). */
export function getLang(): Lang {
  return currentLang;
}

/**
 * Switch locale. Persists to localStorage, mirrors to `<html lang>`, and
 * fan-outs to every `useLang()` subscriber.
 *
 * Unknown locales are silently coerced to "en" so a tampered
 * localStorage value can't poison the next session.
 */
export function setLang(lang: Lang): void {
  const next: Lang = SUPPORTED.has(lang) ? lang : "en";
  if (next === currentLang) return;
  currentLang = next;
  try {
    if (typeof localStorage !== "undefined") {
      localStorage.setItem(I18N_KEY, next);
    }
  } catch {
    /* storage blocked — keep in-memory value only */
  }
  if (typeof document !== "undefined" && document.documentElement) {
    document.documentElement.setAttribute("lang", next);
  }
  notify();
}

/**
 * Lookup a translated string for the current locale. Falls back to the
 * English string when the key is missing in the active locale; falls
 * back to the key itself when both locales lack it. Never returns
 * "undefined" — easier to spot bad keys when reading the DOM.
 */
export function t(key: string): string {
  return (
    STRINGS[currentLang]?.[key] ??
    STRINGS.en[key] ??
    key
  );
}

/**
 * React hook: subscribes to locale changes so the component re-renders
 * when the user flips between English and Turkish. Returns the current
 * locale id — components that just need the side-effect of re-render
 * can ignore the return value.
 */
export function useLang(): Lang {
  return useSyncExternalStore(subscribe, getLang, getLang);
}

/** Test-only escape hatch — used by i18n.test.ts to reset state. */
export function _testResetLang(): void {
  currentLang = "en";
  subscribers.clear();
}

/**
 * Test-only inspection of the keys defined for a given locale.
 *
 * Used by the parity test in i18n.test.ts to catch the silent
 * EN-fallthrough trap: t() falls back to the English string when
 * a key is missing in the active locale, which is great for runtime
 * (no "undefined" in the DOM) but masks translation gaps. By
 * comparing the sorted key sets of "en" and "tr" we can fail CI
 * the moment a developer adds an English string without a Turkish
 * counterpart (or vice versa), forcing the translation deficit
 * into the diff rather than letting it leak into production.
 *
 * Intentionally returns a fresh array on each call so callers can
 * sort/mutate without touching the module-private dictionary.
 */
export function _testKeysForLocale(lang: Lang): string[] {
  return Object.keys(STRINGS[lang] ?? {});
}

// Side-effect: mirror initial locale to `<html lang>` so screen readers
// pick the right language as soon as the bundle parses. Guarded for
// SSR / jsdom edge cases.
if (typeof document !== "undefined" && document.documentElement) {
  document.documentElement.setAttribute("lang", currentLang);
}
