import { SettingsSubPage } from "./settings-sub-page";
import { EmailDeliveryPanel } from "./settings-panels";

const SAVE_KEYS = [
  "email_provider_type",
  "email_from_name", "email_from_address", "email_reply_to",
  "email_smtp_host", "email_smtp_port", "email_smtp_username", "email_smtp_password",
  "email_imap_host", "email_imap_port", "email_imap_username", "email_imap_password",
  "email_use_tls",
  "email_auto_send_enabled", "email_require_approval_before_send",
  "email_reply_detection_enabled", "email_reply_check_interval_seconds",
];

export function SmtpSettingsPage() {
  return (
    <SettingsSubPage
      title="SMTP / IMAP 发信配置（兜底）"
      description="默认发件 Provider 选 SMTP/IMAP 时使用这里的账号。新方案建议用 Microsoft Graph（共享邮箱）替代。"
      saveKeys={SAVE_KEYS}
    >
      {({ values, handleChange }) => (
        <EmailDeliveryPanel
          values={values}
          onChange={handleChange}
          onPersist={async () => {/* saving handled by SettingsSubPage footer */}}
        />
      )}
    </SettingsSubPage>
  );
}
