import { Component, type ErrorInfo, type ReactNode } from "react";
import { Link } from "react-router-dom";

interface State {
  hasError: boolean;
  error: Error | null;
}

/**
 * Route-level error boundary. Wrap the <Outlet /> with this so a crashing
 * page shows a recoverable fallback instead of a white-screen-of-death.
 *
 * Key the boundary by `location.pathname` in Shell.tsx so navigating away
 * remounts (and therefore resets) the boundary; otherwise once the error
 * fallback is shown, plain route changes won't clear the error state.
 *
 * Only catches *synchronous* render-time exceptions — async failures
 * (data fetches, queries) flow through React Query's error state and
 * should be handled there.
 */
export class RouteErrorBoundary extends Component<{ children: ReactNode }, State> {
  state: State = { hasError: false, error: null };

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // Log to console — Tauri/native context will surface in dev tools / log file
    // eslint-disable-next-line no-console
    console.error("Route crash:", error, info);
  }

  reset = () => {
    this.setState({ hasError: false, error: null });
  };

  copy = () => {
    if (!this.state.error || typeof window === "undefined") return;
    const text = `${this.state.error.name}: ${this.state.error.message}\n${this.state.error.stack ?? ""}`;
    if (navigator.clipboard?.writeText) {
      void navigator.clipboard
        .writeText(text)
        .catch(() => window.prompt("Copy diagnostics", text));
      return;
    }
    window.prompt("Copy diagnostics", text);
  };

  render() {
    if (!this.state.hasError) return this.props.children;

    return (
      <section
        role="alert"
        className="relative overflow-hidden rounded-xl border border-bad/40 hero-gradient p-6"
      >
        <div className="relative">
          <div className="text-[10px] uppercase tracking-[0.25em] text-bad font-semibold">
            Page crash · unhandled exception
          </div>
          <h1 className="text-lg font-semibold mt-0.5 tracking-tight">
            This page hit an unexpected error
          </h1>
          <p className="mt-2 text-[12px] text-[color:var(--fg-dim)] max-w-prose">
            Catchem caught a JavaScript error inside this page. Your data and other pages are unaffected.
            Use the button below to recover, or refresh the app.
          </p>
          {this.state.error && (
            <details className="mt-4 text-[10px] text-[color:var(--fg-muted)]">
              <summary className="cursor-pointer focus:outline-none focus-visible:ring-1 focus-visible:ring-accent rounded-sm">Show error details</summary>
              <pre className="mt-2 overflow-auto max-h-48 bg-[color:var(--bg-elev2)] rounded p-2 font-mono leading-relaxed">
                {this.state.error.name}: {this.state.error.message}
                {this.state.error.stack ? `\n\n${this.state.error.stack}` : ""}
              </pre>
            </details>
          )}
          <div className="mt-4 flex gap-2">
            <button onClick={this.reset} className="btn focus:outline-none focus-visible:ring-1 focus-visible:ring-accent">Retry this page</button>
            <Link to="/" className="btn focus:outline-none focus-visible:ring-1 focus-visible:ring-accent">Back to overview</Link>
            <button onClick={this.copy} className="btn focus:outline-none focus-visible:ring-1 focus-visible:ring-accent">Copy diagnostics</button>
          </div>
        </div>
      </section>
    );
  }
}
