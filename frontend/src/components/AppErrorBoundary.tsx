import { Component, type ErrorInfo, type ReactNode } from "react";

interface State {
  hasError: boolean;
  error: Error | null;
}

/**
 * Top-level (app-wide) error boundary. Wraps the whole Routes tree so even
 * if the Shell itself crashes the user sees a recoverable message instead
 * of a blank page. More severe copy than RouteErrorBoundary because at this
 * level the in-app navigation is itself unavailable — only option is to
 * reload the app.
 */
export class AppErrorBoundary extends Component<{ children: ReactNode }, State> {
  state: State = { hasError: false, error: null };

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // eslint-disable-next-line no-console
    console.error("App crash:", error, info);
  }

  reload = () => {
    if (typeof window !== "undefined") window.location.reload();
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
        className="min-h-screen flex items-center justify-center p-6"
      >
        <div className="relative max-w-lg w-full overflow-hidden rounded-xl border border-bad/40 hero-gradient p-6">
          <div className="relative">
            <div className="text-[10px] uppercase tracking-[0.25em] text-bad font-semibold">
              Catchem crashed · fatal exception
            </div>
            <h1 className="text-lg font-semibold mt-0.5 tracking-tight">
              The app hit an unrecoverable error
            </h1>
            <p className="mt-2 text-[12px] text-[color:var(--fg-dim)] max-w-prose">
              Catchem caught a top-level JavaScript error that took down the workstation.
              Reload the app to recover; persisted data on disk is unaffected.
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
              <button onClick={this.reload} className="btn focus:outline-none focus-visible:ring-1 focus-visible:ring-accent">Reload app</button>
              <button onClick={this.copy} className="btn focus:outline-none focus-visible:ring-1 focus-visible:ring-accent">Copy diagnostics</button>
            </div>
          </div>
        </div>
      </section>
    );
  }
}
