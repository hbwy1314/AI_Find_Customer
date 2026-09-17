import React from "react";
import ReactDOM from "react-dom/client";
import { RouterProvider, createRouter } from "@tanstack/react-router";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { routeTree } from "./routes/routeTree";
import "./index.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: false,
      retry: 1,
    },
  },
});

const router = createRouter({ routeTree });

class AppErrorBoundary extends React.Component<
  { children: React.ReactNode },
  { hasError: boolean }
> {
  state = { hasError: false };

  static getDerivedStateFromError() {
    return { hasError: true };
  }

  componentDidCatch(error: Error) {
    console.error("[app] failed to load route chunk", error);
  }

  render() {
    if (!this.state.hasError) return this.props.children;
    return (
      <div className="flex min-h-screen flex-col items-center justify-center gap-3 text-muted-foreground">
        <p>页面加载失败，请刷新后重试。</p>
        <button
          type="button"
          className="rounded-md border px-3 py-2 text-sm text-foreground hover:bg-muted"
          onClick={() => window.location.reload()}
        >
          刷新页面
        </button>
      </div>
    );
  }
}

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <React.Suspense
        fallback={<div className="flex min-h-screen items-center justify-center text-muted-foreground">正在加载页面…</div>}
      >
        <AppErrorBoundary>
          <RouterProvider router={router} />
        </AppErrorBoundary>
      </React.Suspense>
    </QueryClientProvider>
  </React.StrictMode>
);
