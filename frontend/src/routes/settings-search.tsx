import { Search } from "lucide-react";
import { SettingsSubPage } from "./settings-sub-page";
import { FieldGroup, SEARCH_FIELDS, EMAIL_FIELDS } from "./settings-panels";
import { Mail } from "lucide-react";

const SAVE_KEYS = [
  ...SEARCH_FIELDS.map((f) => f.key),
  ...EMAIL_FIELDS.map((f) => f.key),
];

export function SearchSettingsPage() {
  return (
    <SettingsSubPage
      title="搜索 & 邮箱工具 API"
      description="Tavily / Serper / Jina / Amap / Baidu / Hunter 等外部 API Key 配置。"
      saveKeys={SAVE_KEYS}
    >
      {({ values, handleChange }) => (
        <div className="space-y-6">
          <FieldGroup
            title="搜索 API 密钥"
            icon={<Search className="h-5 w-5 text-primary" />}
            description="Tavily 用于通用网页检索，Serper 用于 Google Maps 与部分补充查询。"
            fields={SEARCH_FIELDS}
            values={values}
            onChange={handleChange}
          />
          <FieldGroup
            title="邮箱工具"
            icon={<Mail className="h-5 w-5 text-primary" />}
            fields={EMAIL_FIELDS}
            values={values}
            onChange={handleChange}
          />
        </div>
      )}
    </SettingsSubPage>
  );
}
