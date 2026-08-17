import { RefreshCw } from "lucide-react";
import { SettingsSubPage } from "./settings-sub-page";
import { FieldGroup, CONCURRENCY_FIELDS } from "./settings-panels";

const SAVE_KEYS = CONCURRENCY_FIELDS.map((f) => f.key);

export function PerformanceSettingsPage() {
  return (
    <SettingsSubPage
      title="性能 & 限速参数"
      description="根据你的 API 限流情况调整并发与 RPM。0 表示不限。"
      saveKeys={SAVE_KEYS}
    >
      {({ values, handleChange }) => (
        <FieldGroup
          title="并发与 RPM"
          icon={<RefreshCw className="h-5 w-5 text-primary" />}
          description="影响 LLM 搜索、抓取与邮件生成的最大并发。"
          fields={CONCURRENCY_FIELDS}
          values={values}
          onChange={handleChange}
        />
      )}
    </SettingsSubPage>
  );
}
