import { createRootRoute, createRoute } from "@tanstack/react-router";
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
import { SmtpSettingsPage } from "./settings-smtp";
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

