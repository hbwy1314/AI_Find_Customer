import { createRootRoute, createRoute, redirect } from "@tanstack/react-router";
import { lazy } from "react";
import { RootLayout } from "./root";

const DashboardPage = lazy(() => import("./dashboard").then((module) => ({ default: module.DashboardPage })));
const NewHuntPage = lazy(() => import("./new-hunt").then((module) => ({ default: module.NewHuntPage })));
const HuntDetailPage = lazy(() => import("./hunt-detail").then((module) => ({ default: module.HuntDetailPage })));
const AutomationJobPage = lazy(() => import("./automation-job").then((module) => ({ default: module.AutomationJobPage })));
const QuotasPage = lazy(() => import("./quotas").then((module) => ({ default: module.QuotasPage })));
const LoginPage = lazy(() => import("./login").then((module) => ({ default: module.LoginPage })));
const SignupPage = lazy(() => import("./signup").then((module) => ({ default: module.SignupPage })));
const SettingsLayout = lazy(() => import("./settings-layout").then((module) => ({ default: module.SettingsLayout })));
const SettingsPage = lazy(() => import("./settings").then((module) => ({ default: module.SettingsPage })));
const LLMSettingsPage = lazy(() => import("./settings-llm").then((module) => ({ default: module.LLMSettingsPage })));
const GraphSettingsPage = lazy(() => import("./settings-graph").then((module) => ({ default: module.GraphSettingsPage })));
const SearchSettingsPage = lazy(() => import("./settings-search").then((module) => ({ default: module.SearchSettingsPage })));
const NotificationsSettingsPage = lazy(() => import("./settings-notifications").then((module) => ({ default: module.NotificationsSettingsPage })));
const PerformanceSettingsPage = lazy(() => import("./settings-performance").then((module) => ({ default: module.PerformanceSettingsPage })));
const EmailTestPage = lazy(() => import("./settings-email-test").then((module) => ({ default: module.EmailTestPage })));
const WorkflowSettingsPage = lazy(() => import("./settings-workflow").then((module) => ({ default: module.WorkflowSettingsPage })));
const ConnectedMailboxesPage = lazy(() => import("./connected-mailboxes").then((module) => ({ default: module.ConnectedMailboxesPage })));

// All routes in this file are real pages that exist in the
// repository. The /settings/* routes are children of a
// SettingsLayout that renders a left sidebar + right <Outlet />.

// Catch invalid paths and send the user somewhere useful. TanStack
// Router treats trailing slashes as distinct from the no-slash
// form, so /hunts/ and /hunts both 404 even though the parent
// route is real. /hunts itself is also unused (hunts are listed
// on the dashboard) so it bounces to /.
const KNOWN_ROUTES = new Set([
  "/",
  "/hunts/new",
  "/settings",
  "/quotas",
  "/login",
  "/signup",
]);
const KNOWN_SETTINGS_ROUTES = new Set([
  "llm",
  "graph",
  "search",
  "notifications",
  "performance",
  "email-test",
  "workflow",
  "mailboxes",
]);
function isKnownPrefix(path: string): boolean {
  if (KNOWN_ROUTES.has(path)) return true;
  const parts = path.split("/").filter(Boolean);
  if (parts.length === 2 && parts[0] === "hunts" && parts[1] !== "new") return true;
  if (parts.length === 2 && parts[0] === "automation" && parts[1]) return true;
  if (parts.length === 2 && parts[0] === "settings" && KNOWN_SETTINGS_ROUTES.has(parts[1])) return true;
  return false;
}

const rootRoute = createRootRoute({
  component: RootLayout,
  beforeLoad: ({ location }) => {
    const path = location.pathname;
    // Strip a stray trailing slash (TanStack Router treats
    // /hunts/ and /hunts as different routes).
    if (path.length > 1 && path.endsWith("/")) {
      throw redirect({ to: path.slice(0, -1) as "/", replace: true });
    }
    // Any other unknown top-level path lands on the dashboard.
    if (!isKnownPrefix(path)) {
      throw redirect({ to: "/", replace: true });
    }
  },
});

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

// Real login + signup pages — these existed in main and were
// already linked from the auth flow, so just register them.
const loginRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/login",
  component: LoginPage,
});

const signupRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/signup",
  component: SignupPage,
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
    graphSettingsRoute,
    searchSettingsRoute,
    notificationsSettingsRoute,
    performanceSettingsRoute,
    emailTestRoute,
    workflowSettingsRoute,
    connectedMailboxesRoute,
  ]),
]);
