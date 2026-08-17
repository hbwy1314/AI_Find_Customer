import { createElement, type ReactNode } from "react";
import { createRootRoute, createRoute } from "@tanstack/react-router";
import { RootLayout } from "./root";
import { DashboardPage } from "./dashboard";
import { NewHuntPage } from "./new-hunt";
import { HuntDetailPage } from "./hunt-detail";
import { AutomationJobPage } from "./automation-job";
import { QuotasPage } from "./quotas";
import { SettingsLayout } from "./settings-layout";
import { SettingsPage } from "./settings";
import { LLMSettingsPage } from "./settings-llm";
import { SmtpSettingsPage } from "./settings-smtp";
import { GraphSettingsPage } from "./settings-graph";
import { SearchSettingsPage } from "./settings-search";
import { NotificationsSettingsPage } from "./settings-notifications";
import { PerformanceSettingsPage } from "./settings-performance";
import { EmailTestPage } from "./settings-email-test";
import { WorkflowSettingsPage } from "./settings-workflow";
import { ConnectedMailboxesPage } from "./connected-mailboxes";

// Placeholder pages for routes that the navigation
// (root.tsx / settings.tsx) already links to but whose real
// implementations are still in-flight WIP. Each placeholder
// renders a "coming soon" card so type-checked navigation
// resolves to a sensible destination rather than a 404.
//
// Note: this file is .ts (not .tsx) so we build the placeholder
// markup with createElement rather than JSX.
function PlaceholderCard({ title, hint }: { title: string; hint?: string }): ReactNode {
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
        hint ??
          "此页面是正在进行的重构的一部分，尚未提交到主分支。路径已注册，因此导航链接不会显示 404。",
      ),
    ),
  );
}

function LoginPlaceholder(): ReactNode { return createElement(PlaceholderCard, { title: "登录" }); }
function SignupPlaceholder(): ReactNode { return createElement(PlaceholderCard, { title: "注册" }); }

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

const settingsLayoutRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/settings",
  component: SettingsLayout,
});

// Settings hub: shows status row + nav card grid. The hub nav points
// to the child routes below, which render in <Outlet /> on the right.
const settingsIndexRoute = createRoute({
  getParentRoute: () => settingsLayoutRoute,
  path: "/",
  component: SettingsPage,
});

const llmSettingsRoute = createRoute({
  getParentRoute: () => settingsLayoutRoute,
  path: "/llm",
  component: LLMSettingsPage,
});

const smtpSettingsRoute = createRoute({
  getParentRoute: () => settingsLayoutRoute,
  path: "/smtp",
  component: SmtpSettingsPage,
});

const graphSettingsRoute = createRoute({
  getParentRoute: () => settingsLayoutRoute,
  path: "/graph",
  component: GraphSettingsPage,
});

const searchSettingsRoute = createRoute({
  getParentRoute: () => settingsLayoutRoute,
  path: "/search",
  component: SearchSettingsPage,
});

const notificationsSettingsRoute = createRoute({
  getParentRoute: () => settingsLayoutRoute,
  path: "/notifications",
  component: NotificationsSettingsPage,
});

const performanceSettingsRoute = createRoute({
  getParentRoute: () => settingsLayoutRoute,
  path: "/performance",
  component: PerformanceSettingsPage,
});

const emailTestRoute = createRoute({
  getParentRoute: () => settingsLayoutRoute,
  path: "/email-test",
  component: EmailTestPage,
});

const workflowSettingsRoute = createRoute({
  getParentRoute: () => settingsLayoutRoute,
  path: "/workflow",
  component: WorkflowSettingsPage,
});

const connectedMailboxesRoute = createRoute({
  getParentRoute: () => settingsLayoutRoute,
  path: "/mailboxes",
  component: ConnectedMailboxesPage,
});

// Placeholders for routes already linked from the nav / hub
// but whose real implementations are WIP. Keep these so the
// type-checked <Link to="..."> calls compile, and so navigating
// to them doesn't 404.
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
  component: QuotasPage,
});

export const routeTree = rootRoute.addChildren([
  indexRoute,
  loginRoute,
  signupRoute,
  newHuntRoute,
  huntDetailRoute,
  automationJobRoute,
  quotasRoute,
  settingsLayoutRoute.addChildren([
    settingsIndexRoute,
    llmSettingsRoute,
    smtpSettingsRoute,
    graphSettingsRoute,
    searchSettingsRoute,
    notificationsSettingsRoute,
    performanceSettingsRoute,
    emailTestRoute,
    workflowSettingsRoute,
    connectedMailboxesRoute,
  ]),
]);

