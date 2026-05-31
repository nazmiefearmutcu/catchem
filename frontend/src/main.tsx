import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter } from "react-router-dom";
import { App } from "@/app/App";
import "@/styles/globals.css";

// React-Query retry policy is the second line of defence behind the
// SidecarBanner. The banner handles user-visible "Reconnecting…" via
// /healthz polling; the retry here makes sure transient network blips
// (sidecar restarts, single dropped fetch) self-heal without surfacing
// an error UI at all. We cap at 3 attempts with exponential backoff
// (1s → 2s → 4s, hard ceiling 8s) and keep 4xx errors fail-fast — only
// network errors and 5xx benefit from retry. ApiError carries `status`,
// so a 404 from a malformed query key still fails immediately.
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: false,
      staleTime: 5_000,
      retry: (failureCount, error) => {
        // ApiError exposes the HTTP status; everything else (TypeError
        // network errors, AbortError) is treated as transient.
        const status = (error as { status?: number } | null)?.status;
        if (typeof status === "number" && status >= 400 && status < 500) {
          return false;
        }
        return failureCount < 3;
      },
      retryDelay: (attempt) => Math.min(1_000 * 2 ** attempt, 8_000),
    },
  },
});

const root = document.getElementById("root");
if (!root) throw new Error("missing #root element");

ReactDOM.createRoot(root).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter future={{ v7_relativeSplatPath: true, v7_startTransition: true }}>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>
);
