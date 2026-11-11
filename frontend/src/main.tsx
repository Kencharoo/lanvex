import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode, Suspense, lazy } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, RouterProvider } from "react-router";
import { Toaster } from "sonner";
import { AppLayout } from "@/app/layout";
import { SessionGate } from "@/app/session";
import { ThemeProvider, useTheme } from "@/app/theme";
import { LogoMark } from "@/components/logo";
import { PageHeader } from "@/components/page";
import { Skeleton } from "@/components/ui/misc";
import { TooltipProvider } from "@/components/ui/misc";
import { ApiError } from "@/lib/api";
import { ActivityPage } from "@/pages/activity";
import { DeviceDetailPage, DevicesPage } from "@/pages/devices";
import { IPAddressesPage } from "@/pages/ip-addresses";
import { LoginPage } from "@/pages/login";
import { NetworksPage } from "@/pages/networks";
import { AutomationPage, TopologyPage } from "@/pages/planned";
import { ServersPage } from "@/pages/servers";
import { ServerDetailPage } from "@/pages/servers/detail";
import { SettingsPage } from "@/pages/settings";
import { VLANsPage } from "@/pages/vlans";
import "./index.css";

// Recharts is the heaviest dependency; only the overview needs it.
const OverviewPage = lazy(() => import("@/pages/overview").then((m) => ({ default: m.OverviewPage })));

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 15_000,
      refetchOnWindowFocus: false,
      retry: (count, e) => !(e instanceof ApiError && e.status >= 400 && e.status < 500) && count < 2,
    },
  },
});

function NotFound() {
  return <PageHeader title="Page not found" description="The address may be mistyped, or the page has moved." />;
}

const router = createBrowserRouter([
  {
    element: <AppLayout />,
    children: [
      { index: true, element: <Suspense fallback={<Skeleton className="h-24 w-full" />}><OverviewPage /></Suspense> },
      { path: "networks", element: <NetworksPage /> },
      { path: "ip-addresses", element: <IPAddressesPage /> },
      { path: "vlans", element: <VLANsPage /> },
      { path: "devices", element: <DevicesPage /> },
      { path: "devices/:id", element: <DeviceDetailPage /> },
      { path: "servers", element: <ServersPage /> },
      { path: "servers/:id", element: <ServerDetailPage /> },
      { path: "topology", element: <TopologyPage /> },
      { path: "automation", element: <AutomationPage /> },
      { path: "activity", element: <ActivityPage /> },
      { path: "settings", element: <SettingsPage /> },
      { path: "*", element: <NotFound /> },
    ],
  },
]);

function Loading() {
  return (
    <div className="grid min-h-dvh place-items-center" aria-busy="true" aria-label="Loading Lanvex">
      <LogoMark className="size-8 animate-pulse" />
    </div>
  );
}

function ThemedToaster() {
  const { theme } = useTheme();
  return <Toaster theme={theme} position="bottom-right" toastOptions={{ className: "font-sans text-[13px]" }} />;
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ThemeProvider>
      <QueryClientProvider client={queryClient}>
        <TooltipProvider delayDuration={300}>
          <SessionGate signIn={<LoginPage />} loading={<Loading />}>
            <RouterProvider router={router} />
          </SessionGate>
          <ThemedToaster />
        </TooltipProvider>
      </QueryClientProvider>
    </ThemeProvider>
  </StrictMode>,
);
