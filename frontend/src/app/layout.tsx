import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  Boxes,
  ChevronsLeft,
  ChevronsRight,
  Cpu,
  LayoutGrid,
  LogOut,
  Moon,
  Network,
  Server,
  Settings,
  Radar,
  Share2,
  Sun,
  SunMoon,
  Tags,
  Workflow,
  Waypoints,
} from "lucide-react";
import * as React from "react";
import { Link, NavLink, Outlet, useLocation } from "react-router";
import { Wordmark } from "@/components/logo";
import { Menu, MenuContent, MenuItem, MenuSeparator, MenuTrigger, Tooltip } from "@/components/ui/misc";
import { api } from "@/lib/api";
import type { ChangeOut, NetBoxStatus } from "@/lib/types";
import { useNetBoxLabel, useSystemInfo } from "@/lib/system";
import { cn } from "@/lib/utils";
import { useSession } from "./session";
import { type ThemePref, useTheme } from "./theme";

type NavItem = {
  to: string;
  label: string;
  icon: React.ComponentType<{ className?: string }>;
  later?: boolean;
  also?: string;
  /** Not part of this app: a full page load (Lanvex Core under /core/). */
  page?: boolean;
  /** Shown only when Lanvex Core is part of this installation. */
  core?: boolean;
};

const NAV: NavItem[][] = [
  [{ to: "/", label: "Overview", icon: LayoutGrid }],
  [
    { to: "/networks", label: "Networks", icon: Network },
    { to: "/ip-addresses", label: "IP Addresses", icon: Waypoints },
    { to: "/vlans", label: "VLANs", icon: Tags },
    { to: "/devices", label: "Devices", icon: Cpu },
    { to: "/servers", label: "Servers", icon: Server },
    { to: "/core/", label: "Lanvex Core", icon: Boxes, page: true, core: true },
  ],
  [
    { to: "/topology", label: "Topology", icon: Share2 },
    { to: "/discovery", label: "Discovery", icon: Radar },
    { to: "/automation", label: "Automation", icon: Workflow, also: "/changes/" },
    { to: "/activity", label: "Activity & Audit Logs", icon: Activity },
  ],
  [{ to: "/settings", label: "Settings", icon: Settings }],
];

const COLLAPSE_KEY = "lanvex-sidebar-collapsed";

function useNetBoxStatus() {
  return useQuery({
    queryKey: ["netbox-status"],
    queryFn: () => api<NetBoxStatus>("/system/netbox"),
    refetchInterval: 60_000,
    staleTime: 30_000,
  });
}

function ConnectionIndicator({ collapsed }: { collapsed: boolean }) {
  const { data, isPending } = useNetBoxStatus();
  const nbName = useNetBoxLabel();
  const state = isPending
    ? { tone: "bg-faint", text: `Checking ${nbName}…` }
    : !data?.configured
      ? { tone: "bg-faint", text: `${nbName} not configured` }
      : !data.reachable
        ? { tone: "bg-danger", text: `${nbName} unreachable` }
        : !data.authenticated
          ? { tone: "bg-warn", text: `${nbName} token rejected` }
          : data.supported === false
            ? { tone: "bg-warn", text: `NetBox ${data.version} unsupported` }
            : { tone: "bg-ok", text: nbName === "NetBox" ? `NetBox ${data.version}` : `${nbName} · NetBox ${data.version}` };
  const content = (
    <NavLink
      to="/settings"
      className="flex items-center gap-2 rounded-[var(--radius-control)] px-2 py-1.5 text-xs text-muted hover:bg-surface-2 hover:text-ink"
    >
      <span className={cn("size-2 shrink-0 rounded-full", state.tone)} aria-hidden />
      {!collapsed && <span className="truncate">{state.text}</span>}
      {collapsed && <span className="sr-only">{state.text}</span>}
    </NavLink>
  );
  return collapsed ? <Tooltip content={state.text} side="right">{content}</Tooltip> : content;
}

function UserMenu({ collapsed }: { collapsed: boolean }) {
  const { user, signOut } = useSession();
  const { theme, setTheme } = useTheme();
  const themes: { v: ThemePref; label: string; icon: typeof Sun }[] = [
    { v: "light", label: "Light", icon: Sun },
    { v: "dark", label: "Dark", icon: Moon },
    { v: "system", label: "Match system", icon: SunMoon },
  ];
  const initials = (user.display_name || user.username).slice(0, 2).toUpperCase();
  return (
    <Menu>
      <MenuTrigger className="flex w-full items-center gap-2 rounded-[var(--radius-control)] px-2 py-1.5 text-left hover:bg-surface-2">
        <span className="grid size-6 shrink-0 place-items-center rounded-full bg-accent-soft text-2xs font-semibold text-accent">{initials}</span>
        {!collapsed && (
          <span className="min-w-0 flex-1">
            <span className="block truncate text-[13px] font-medium">{user.display_name || user.username}</span>
            <span className="block truncate text-xs text-muted">{user.display_name ? user.username : user.role === "admin" ? "Administrator" : "Read-only"}</span>
          </span>
        )}
      </MenuTrigger>
      <MenuContent align="start" side="top" className="w-52">
        {themes.map((t) => (
          <MenuItem key={t.v} onSelect={() => setTheme(t.v)} aria-checked={theme === t.v} role="menuitemradio">
            <t.icon /> {t.label}
            {theme === t.v && <span className="ml-auto size-1.5 rounded-full bg-accent" aria-hidden />}
          </MenuItem>
        ))}
        <MenuSeparator />
        <MenuItem onSelect={() => void signOut()}>
          <LogOut /> Sign out
        </MenuItem>
      </MenuContent>
    </Menu>
  );
}

export function AppLayout() {
  const [collapsed, setCollapsed] = React.useState(() => {
    try {
      return localStorage.getItem(COLLAPSE_KEY) === "1";
    } catch {
      return false;
    }
  });
  const toggle = () => {
    setCollapsed((c) => {
      try {
        localStorage.setItem(COLLAPSE_KEY, c ? "0" : "1");
      } catch {
        /* ignore */
      }
      return !c;
    });
  };
  const location = useLocation();
  const systemInfo = useSystemInfo();
  const mainRef = React.useRef<HTMLElement>(null);
  React.useEffect(() => {
    mainRef.current?.focus({ preventScroll: true });
  }, [location.pathname]);

  return (
    <div className="flex min-h-dvh">
      <a href="#main" className="sr-only focus:not-sr-only focus:fixed focus:left-2 focus:top-2 focus:z-50 focus:rounded focus:bg-surface focus:px-3 focus:py-2">
        Skip to content
      </a>
      <aside
        className={cn(
          "sticky top-0 flex h-dvh shrink-0 flex-col border-r border-line bg-surface transition-[width] duration-150",
          collapsed ? "w-14" : "w-60",
        )}
      >
        <div className={cn("flex h-12 items-center border-b border-line", collapsed ? "justify-center" : "justify-between px-3")}>
          <NavLink to="/" aria-label="Lanvex overview">
            <Wordmark collapsed={collapsed} />
          </NavLink>
          {!collapsed && (
            <button onClick={toggle} className="rounded p-1 text-faint hover:bg-surface-2 hover:text-ink" aria-label="Collapse sidebar">
              <ChevronsLeft className="size-4" />
            </button>
          )}
        </div>
        <nav aria-label="Main" className="flex-1 overflow-y-auto px-2 py-3">
          {NAV.map((group, gi) => (
            <ul key={gi} className={cn("grid gap-px", gi > 0 && "mt-3 border-t border-line pt-3")}>
              {group.filter((item) => !item.core || systemInfo.data?.core_enabled).map((item) => {
                const link = item.page ? (
                  <a
                    href={item.to}
                    className={cn(
                      "group relative flex h-8 items-center gap-2.5 rounded-[var(--radius-control)] px-2 text-[13px] text-muted transition-colors hover:bg-surface-2 hover:text-ink",
                      collapsed && "justify-center px-0",
                    )}
                  >
                    <item.icon className="size-4 shrink-0" />
                    {!collapsed && <span className="flex-1 truncate">{item.label}</span>}
                    {collapsed && <span className="sr-only">{item.label}</span>}
                  </a>
                ) : (
                  <NavLink
                    to={item.to}
                    end={item.to === "/"}
                    className={({ isActive }) =>
                      cn(
                        "group relative flex h-8 items-center gap-2.5 rounded-[var(--radius-control)] px-2 text-[13px] transition-colors",
                        isActive || (item.also && location.pathname.startsWith(item.also)) ? "bg-accent-soft font-medium text-accent" : "text-muted hover:bg-surface-2 hover:text-ink",
                        collapsed && "justify-center px-0",
                      )
                    }
                  >
                    <item.icon className="size-4 shrink-0" />
                    {!collapsed && <span className="flex-1 truncate">{item.label}</span>}
                    {!collapsed && item.later && <span className="text-2xs text-faint">Planned</span>}
                    {collapsed && <span className="sr-only">{item.label}</span>}
                  </NavLink>
                );
                return <li key={item.to}>{collapsed ? <Tooltip content={item.label} side="right">{link}</Tooltip> : link}</li>;
              })}
            </ul>
          ))}
        </nav>
        <div className="grid gap-1 border-t border-line p-2">
          <ConnectionIndicator collapsed={collapsed} />
          <UserMenu collapsed={collapsed} />
          {collapsed && (
            <button onClick={toggle} className="mx-auto rounded p-1 text-faint hover:bg-surface-2 hover:text-ink" aria-label="Expand sidebar">
              <ChevronsRight className="size-4" />
            </button>
          )}
        </div>
      </aside>
      <main id="main" ref={mainRef} tabIndex={-1} className="min-w-0 flex-1 outline-none">
        <div className="mx-auto max-w-[1280px] px-6 py-6 lg:px-8">
          <AwaitingDecision />
          <Outlet />
        </div>
      </main>
    </div>
  );
}


/** Changes applied on a server Lanvex runs on wait for an administrator to keep them in the browser. If
 * nobody does in time, the server undoes them, so they are shown on every page, not only their own. */
function AwaitingDecision() {
  const { isAdmin: admin } = useSession();
  const { pathname } = useLocation();
  const q = useQuery({
    queryKey: ["changes", "awaiting-browser"],
    queryFn: () => api<ChangeOut[]>("/changes?status=verifying&limit=20"),
    enabled: admin,
    refetchInterval: 4000,
  });
  const waiting = (q.data ?? []).filter((c) => c.awaiting_browser && !pathname.startsWith(`/changes/${c.id}`));
  if (!admin || waiting.length === 0) return null;
  return (
    <div className="mb-4 grid gap-2">
      {waiting.map((c) => (
        <div key={c.id} role="alert" className="flex flex-wrap items-center justify-between gap-2 rounded-[var(--radius-control)] border-2 border-accent bg-surface px-3 py-2 text-[13px]">
          <span>
            <strong>Change #{c.number} on {c.server_name}</strong> is applied and waits for you to keep it. Without a decision, the server undoes it shortly.
          </span>
          <Link to={`/changes/${c.id}`} className="font-medium text-accent hover:underline">
            Keep or undo
          </Link>
        </div>
      ))}
    </div>
  );
}
