import { createElement, type ReactNode } from "react";
import { createRootRoute, createRoute } from "@tanstack/react-router";
import { RootLayout } from "./root";
import { DashboardPage } from "./dashboard";
import { NewHuntPage } from "./new-hunt";
import { HuntDetailPage } from "./hunt-detail";
import { AutomationJobPage } from "./automation-job";
import { SettingsPage } from "./settings";
import { WorkflowSettingsPage } from "./settings-workflow";

// Placeholder pages for routes already linked from root.tsx /
// settings.tsx but whose real implementations are still in-flight
// WIP. Each placeholder renders a "coming soon" card so type-
// checked navigation still resolves. Replace with the real page
// once the WIP lands on main.
//
// Note: this file is .ts (not .tsx) so we build the placeholder
// markup with createElement rather than JSX. Keeps the import
// surface flat — no .tsx file is forced on us just for the
// placeholders.
function PlaceholderCard({ title }: { title: string }): ReactNode {
  return createElement(
    "div",
    { className: "max-w-3xl mx-auto p-6" },
    createElement(
      "div",
      { className: "rounded-lg border bg-card p-8 text-card-foreground shadow-sm" },
      createElement("h1", { className: "text-2xl font-bold tracking-tight" }, title),
      createElement(
        "p",
        { className: "mt-2 text-muted-foreground" },
        "This page is part of an in-flight rewrite and hasn't been committed yet. " +
          "The route is registered so existing navigation links resolve to a sensible destination.",
      ),
    ),
  );
}

function LoginPlaceholder(): ReactNode { return createElement(PlaceholderCard, { title: "登录" }); }
function SignupPlaceholder(): ReactNode { return createElement(PlaceholderCard, { title: "注册" }); }
function QuotasPlaceholder(): ReactNode { return createElement(PlaceholderCard, { title: "发件配额" }); }
function MailboxesPlaceholder(): ReactNode { return createElement(PlaceholderCard, { title: "已连接邮箱" }); }

const rootRoute = createRootRoute({ component: RootLayout });

const indexRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  component: DashboardPage,
});

const newHuntRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/hunts/new",
  component: NewHuntPage,
});

const huntDetailRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/hunts/$huntId",
  component: HuntDetailPage,
});

const automationJobRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/automation/$jobId",
  component: AutomationJobPage,
});

const settingsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/settings",
  component: SettingsPage,
});

const workflowSettingsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/settings/workflow",
  component: WorkflowSettingsPage,
});

const loginRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/login",
  component: LoginPlaceholder,
});

const signupRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/signup",
  component: SignupPlaceholder,
});

const quotasRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/quotas",
  component: QuotasPlaceholder,
});

const connectedMailboxesRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/settings/mailboxes",
  component: MailboxesPlaceholder,
});

export const routeTree = rootRoute.addChildren([
  indexRoute,
  loginRoute,
  signupRoute,
  newHuntRoute,
  huntDetailRoute,
  automationJobRoute,
  quotasRoute,
  settingsRoute,
  workflowSettingsRoute,
  connectedMailboxesRoute,
]);
