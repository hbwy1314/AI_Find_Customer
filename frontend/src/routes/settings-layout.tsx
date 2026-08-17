/**
 * Layout for the /settings section. Renders a fixed-width left sidebar with
 * the configuration sub-pages, and renders the current sub-page on the right
 * via <Outlet />. All /settings/* routes are children of this layout in
 * routeTree.ts, so the sidebar is always visible while the user is in the
 * settings area.
 */

import { Link, Outlet, useRouterState } from "@tanstack/react-router";
import {
  Cpu,
  Inbox,
  KeyRound,
  Bell,
  RefreshCw,
  Search,
  Send,
  LayoutDashboard,
} from "lucide-react";
import type { ReactNode } from "react";

type NavItem = {
  to: string;
  label: string;
  icon: ReactNode;
  group?: string;
};

const NAV_ITEMS: NavItem[] = [
  { to: "/settings", label: "总览", icon: <LayoutDashboard className="h-4 w-4" />, group: "配置中心" },
  { to: "/settings/mailboxes", label: "邮箱账号", icon: <Inbox className="h-4 w-4" />, group: "邮件" },
  { to: "/settings/email-test", label: "邮件测试", icon: <Send className="h-4 w-4" />, group: "邮件" },
  { to: "/settings/graph", label: "Microsoft Graph", icon: <KeyRound className="h-4 w-4" />, group: "邮件" },
  { to: "/settings/llm", label: "AI 模型", icon: <Cpu className="h-4 w-4" />, group: "系统" },
  { to: "/settings/search", label: "搜索 API", icon: <Search className="h-4 w-4" />, group: "系统" },
  { to: "/settings/notifications", label: "飞书通知", icon: <Bell className="h-4 w-4" />, group: "系统" },
  { to: "/settings/performance", label: "性能 & 限速", icon: <RefreshCw className="h-4 w-4" />, group: "系统" },
];

function isItemActive(currentPath: string, itemTo: string): boolean {
  // Hub ("/settings") is active only on exact match; sub-pages are active when
  // their prefix matches the current path.
  if (itemTo === "/settings") return currentPath === "/settings";
  return currentPath === itemTo || currentPath.startsWith(itemTo + "/");
}

function groupItems(items: NavItem[]) {
  const groups: { name: string; items: NavItem[] }[] = [];
  for (const item of items) {
    const name = item.group ?? "";
    let bucket = groups.find((g) => g.name === name);
    if (!bucket) {
      bucket = { name, items: [] };
      groups.push(bucket);
    }
    bucket.items.push(item);
  }
  return groups;
}

export function SettingsLayout() {
  const { location } = useRouterState();
  const groups = groupItems(NAV_ITEMS);

  return (
    <div className="max-w-6xl mx-auto px-4 py-6">
      <div className="grid gap-6 md:grid-cols-[224px_1fr]">
        <aside className="md:sticky md:top-16 md:self-start">
          <nav className="rounded-lg border bg-card p-2 text-card-foreground shadow-sm">
            {groups.map((group, gi) => (
              <div key={group.name} className={gi === 0 ? "" : "mt-3 pt-3 border-t"}>
                {group.name ? (
                  <p className="px-3 py-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
                    {group.name}
                  </p>
                ) : null}
                <ul className="space-y-0.5">
                  {group.items.map((item) => {
                    const active = isItemActive(location.pathname, item.to);
                    return (
                      <li key={item.to}>
                        <Link
                          to={item.to}
                          className={`flex items-center gap-2 rounded-md px-3 py-2 text-sm transition-colors ${
                            active
                              ? "bg-primary/10 text-primary font-medium"
                              : "text-muted-foreground hover:bg-muted hover:text-foreground"
                          }`}
                        >
                          {item.icon}
                          {item.label}
                        </Link>
                      </li>
                    );
                  })}
                </ul>
              </div>
            ))}
          </nav>
        </aside>
        <main className="min-w-0">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
