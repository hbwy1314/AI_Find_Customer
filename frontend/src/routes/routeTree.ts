import { createRootRoute, createRoute, redirect } from "@tanstack/react-router";
import { RootLayout } from "./root";
import { DashboardPage } from "./dashboard";
import { NewHuntPage } from "./new-hunt";
import { HuntDetailPage } from "./hunt-detail";
import { AutomationJobPage } from "./automation-job";
import { QuotasPage } from "./quotas";
import { LoginPage } from "./login";
import { SignupPage } from "./signup";
import { SettingsLayout } from "./settings-layout";
import { SettingsPage } from "./settings";
import { LLMSettingsPage } from "./settings-llm";
import { GraphSettingsPage } from "./settings-graph";
import { SearchSettingsPage } from "./settings-search";
import { NotificationsSettingsPage } from "./settings-notifications";
import { PerformanceSettingsPage } from "./settings-performance";
import { EmailTestPage } from "./settings-email-test";
import { WorkflowSettingsPage } from "./settings-workflow";
import { ConnectedMailboxesPage } from "./connected-mailboxes";

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
  "/automation",
  "/settings",
  "/quotas",
  "/login",
  "/signup",
]);
function isKnownPrefix(path: string): boolean {
  if (KNOWN_ROUTES.has(path)) return true;
  for (const known of KNOWN_ROUTES) {
    if (path === known || path.startsWith(known + "/")) return true;
  }
  if (/^\/hunts\/[^/]+/.test(path)) return true; // /hunts/$huntId
  if (/^\/automation\/[^/]+/.test(path)) return true; // /automation/$jobId
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

