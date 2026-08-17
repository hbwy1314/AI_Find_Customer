import { SettingsSubPage } from "./settings-sub-page";
import { AutomationNotifyPanel } from "./settings-panels";

const SAVE_KEYS = [
  "automation_feishu_webhook_url",
  "automation_reply_notifications_enabled",
  "automation_summary_enabled", "automation_summary_interval_seconds",
  "automation_alerts_enabled", "automation_alert_interval_seconds",
  "automation_alert_backlog_threshold", "automation_alert_failed_messages_threshold",
];

export function NotificationsSettingsPage() {
  return (
    <SettingsSubPage
      title="飞书通知 & 异常告警"
      description="实时通知、周期汇总、异常告警都从这里发。"
      saveKeys={SAVE_KEYS}
    >
      {({ values, handleChange }) => (
        <AutomationNotifyPanel
          values={values}
          onChange={handleChange}
          onPersist={async () => {/* handled by SettingsSubPage footer */}}
        />
      )}
    </SettingsSubPage>
  );
}
