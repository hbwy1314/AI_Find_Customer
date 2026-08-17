# Plan — 4 项功能改进

**Date**: 2026-08-17
**Status**: ✅ All 4 phases shipped (commits `f42e139` / `91ca690` / `2d36abb` / `6cdcb58`)
**Author**: Mavis (mavis session)

## 背景

4 个用户报告的产品问题，需要在 sprint 排期内规划清楚再动手。每个问题独立但有依赖关系。

## 问题清单

| # | 问题 | 严重度 | 工作量估时 |
|---|---|---|---|
| 3 | 邮件必须有退订功能（合规） | 🔴 P0 | 0.5-1 天 |
| 4 | Hunter.io 真正接入 | 🟡 P1 | 1-2 天 |
| 1 | 多邮箱时按"3 天无回复换下一个"序列发送 | 🟡 P1 | 1-1.5 天 |
| 2 | AI 邮件必须按模板生成，不能自由发挥 | 🟡 P2 | 0.5-1 天 |

---

## 推荐执行顺序

**3 → 4 → 1 → 2**（按这个顺序的理由见每节末尾的"为什么这个顺序"）

---

## 1. 多邮箱 waterfall 序列（P1）

### 现状

- 数据模型：`lead_email_sequences` 一行 = 一个 lead 在某个 campaign 下的"邮件任务"。一行 sequence 只能发一个收件人。
- `email_messages` 表外键指向 `sequence_id`，一条 sequence 对应 1-3 封跟进邮件（不是 1-3 个收件人）。
- scheduler 跑的时候按 `sequence.email_account_id` 找发件邮箱，按 `sequence.recipient_email` 找收件人——一个收件人发完，等 reply 决定下一步。

**所以现在"多邮箱"是支持的，但没按"换人发"逻辑**。要改造。

### 方案

加一个 `recipient_pool` 概念：一个 lead 可能有 N 个候选邮箱，scheduler 顺序尝试：

```
state machine for each lead:
  pending   → (发送 → waiting_reply)
  waiting_reply → (3 天无 reply → 选下一个邮箱 → pending)
  waiting_reply → (收到 reply → replied)  // 任何邮箱收到都算
  waiting_reply → (手动 stop → stopped)
  全试完还没 reply → exhausted
```

### 关键改动

| 文件 | 改动 |
|---|---|
| `emailing/store.py` | 新表 `lead_email_recipients` (sequence_id, email, status, sent_at, last_attempt_at)；`_ensure_column` 加 `lead_email_sequences.next_recipient_at` / `exhausted` |
| `emailing/scheduler.py` | `_dispatch_one` 改造：取 sequence 当前 `pending` 收件人；发送后置为 `waiting_reply`；reply_detector 命中任意邮箱 → sequence `replied`；3 天无 reply → 选下一个 pending 收件人 |
| `emailing/reply_detector.py` | 把 reply 状态从 sequence 级下沉到 lead 级（同一 lead 任何邮箱收到回复都停 sequence）|
| `api/email_routes.py` | 新增 `POST /sequences/{id}/stop-recipient` 手动跳到下一个邮箱 |
| 前端 `hunt-detail.tsx` | 显示每个 lead 的 recipient 池（当前 active / 等待中 / 已用完）|

### 可配项（settings）

- `email_recipient_waterfall_days`（默认 3）—— 几天没回复换下一个
- `email_recipient_max_per_lead`（默认 3）—— 最多试几个

### 为什么排第 3

- 依赖 Hunter.io（4）做更好的邮箱发现——试 3 个邮箱前先得有质量高的 3 个
- 依赖 unsubscribe（3）——退订的人必须从 waterfall 池里剔除
- schema 改动最大，需要 store 迁移，最好放最后做

---

## 2. AI 邮件按模板生成（P2）

### 现状

- `email_craft_agent.py` 1908 行，复杂
- 已有的 `EMAIL_TEMPLATE_PERSONALIZER_SYSTEM` prompt 要求"keep same tone / CTA / locale / layout"——但用户报告没遵守
- `generation_mode` 字段有 3 种：`personalized` / `template_pool` / `template_pool_personalized`
- LLM 自由度过大、prompt 没硬约束、schema 没校验

### 方案

**双管齐下**：

A. **Prompt 层**：把 template 整段塞到 prompt 顶部，让 LLM 改写时**必须**保留的结构（如"Subject: {{subject}}", "Body 必须有这些段落", "必须包含 {{cta_text}}"）作为强约束。
B. **Schema 层**：template profile 加 `required_tokens` 字段（must_include: [cta_text, sender_name, product_usp]），生成后用代码扫描 body，缺失就 reject 重试。

### 关键改动

| 文件 | 改动 |
|---|---|
| `emailing/template_pipeline.py` | `extract_template_profile` 新增 `required_tokens` 字段提取（扫描模板里的 `{{...}}` 占位符 + 几个硬编码的必填项）|
| `agents/email_craft_agent.py` | `EMAIL_TEMPLATE_PERSONALIZER_SYSTEM` 改写：把"keep"改成"MUST"，加 example 加 few-shot；body 长度上下界 |
| `agents/email_craft_agent.py` | 新增 `_validate_against_template()`：检查 body 包含 required_tokens、字数 80-400、subject 长度、locale 一致 |
| `agents/email_craft_agent.py` | 校验失败时 retry（最多 2 次），全失败就降级到 `template_pool` 模式（用原始模板不发）|

### 验收

- 准备 3 个不同模板（短/中/长，sales/followup/re-engagement）
- 每个模板跑 5 个 lead，比对生成的 body 是否保留核心段落结构
- 之前用过的"原始模板"作为 ground truth，重合度 > 70% 算通过

### 为什么排第 4

- 纯 prompt + 校验逻辑，不依赖其他改造
- 工作量最小但效果最不确定（LLM 行为难精确控制）—— 排在最后，做完前面的再花时间打磨这个

---

## 3. 邮件退订功能（P0，必做）

### 现状

- 邮件发送 `send_email()` 完全没退订字段
- 邮件正文无退订链接
- SMTP header 无 `List-Unsubscribe`
- 无 unsubscribe endpoint
- 无 unsubscribe 存储
- **法律风险：CAN-SPAM / GDPR 都要求每封营销邮件必须可一键退订，不遵守会被投诉封域名**

### 方案

**3 件事一起做**：

1. **每封邮件加退订链接**（正文底部 + List-Unsubscribe header）
2. **后端 endpoint** 处理点击
3. **scheduler 跳过已退订地址**

### 关键改动

| 文件 | 改动 |
|---|---|
| **新建 `emailing/unsubscribe.py`** | 生成 signed token (HMAC over email + ts)；验证 token；记入 `email_unsubscribes` 表 |
| `emailing/store.py` | 新表 `email_unsubscribes (email, scope, unsubscribed_at, token_hash, source)`；`scope` = 'all' / 'campaign_id' / 'sequence_id'；`is_unsubscribed(email, scope)` 查表 |
| `emailing/email_sender.py` | `send_email()` 加 `list_unsubscribe_url`、`list_unsubscribe_post` 参数；自动注入 `List-Unsubscribe: <mailto:...>, <https://...>` header；自动在 body 末尾追加 `\n\n--\n不再接收此类邮件：<url>` |
| `emailing/scheduler.py` | 派发前调 `is_unsubscribed(recipient, sequence_id)`，是的话 skip + 标 `unsubscribed` |
| **新建 `api/unsubscribe_routes.py`** | `GET /api/unsubscribe/{token}` → 验证 + 记录 + 200 + 简单 HTML 确认页（"已退订"）；`POST /api/unsubscribe/{token}` 同样；`mailto:` 退订走 `POST /api/unsubscribe/mailto` 解析 `From: subject=` |
| `emailing/graph_client.py` | Graph 发送时同样注入 header + footer |
| `api/app.py` | mount unsubscribe router |
| **新建前端 `unsubscribe/landing.tsx` 路由** | （或服务端直接返回 HTML，不走 SPA）—— 选后者，简单 |

### Token 设计

```
token = base64url(hmac_sha256(secret, f"{email}|{scope}|{timestamp}")) + "." + timestamp
验证: 重新算 hmac 比对 + 检查 ts < 90 days
```

- 90 天过期（退订链接不需要永久有效）
- secret 存在 settings store 里（`unsubscribe_token_secret`，启动时若空自动生成 32B）
- 不存原始 email 哈希在 token 里（避免泄漏），只存 token hash

### 为什么排第 1

- 🔴 **法律风险**，漏这一项任何新功能（多邮箱、模板、Hunter）发出去都是违规
- 是其他改造的前置：waterfall（1）需要查 unsubscribed 跳过；Hunter（4）找到的邮箱也可能已经退订要过滤
- 工作量小但风险大，先做

### 验收

- 1. 发一封测试邮件，body 末尾有退订链接
- 2. 邮件 header 有 `List-Unsubscribe: <https://...>`
- 3. 点链接 → 看到"已退订"页面 + DB 记录
- 4. 在 scheduler 重跑该 sequence → 标 `unsubscribed` 不发
- 5. 用 mailto 退订（发邮件到 unsubscribe@）→ 后端收到 → 同样记录

---

## 4. Hunter.io 真正接入（P1）

### 现状

- `HUNTER_API_KEY` settings 字段已存在但**完全没人读**
- `backend/tools/email_finder.py` 100% 本地正则
- `email_verifier.py` 应该也是 DNS MX 校验
- `requirements.txt` 没装 `hunter-io` 包
- venv 里没装

### 方案

**3 个 API 接进来，1 个不接**：

| API | 接不接 | 理由 |
|---|---|---|
| **Domain Search** `GET /v2/domain-search` | ✅ 接 | 给域名拿全公司已知邮箱 + 职位，最常用 |
| **Email Verifier** `GET /v2/email-verifier` | ✅ 接 | 比 DNS MX 准，验证 deliverable / risky / undeliverable |
| **Email Finder** `GET /v2/email-finder?domain=&first_name=&last_name=` | ✅ 接 | lead 有人名+域名时构造个人邮箱 |
| **Discover** `GET /v2/discover?query=` | ❌ 不接 | 月配额贵、跟 Brave/Tavily 重复 |
| **Bulk** 各种 | ❌ 不接 | 超额风险 |

### 关键改动

| 文件 | 改动 |
|---|---|
| **新建 `backend/tools/hunter_client.py`** | 4 个 client 方法（虽然只用 3 个）+ 重试（tenacity）+ 配额计数（写 settings 或单独表）；429 退避；余额查询；key 错误返回明确 status |
| `backend/tools/registry.py` | 注册 `HunterEmailFinderTool`（包装 Domain Search + Email Finder）、`HunterVerifierTool`（包装 Email Verifier）|
| `backend/emailing/email_finder.py` | 改造：先本地正则 → 没找到时 fallback Hunter Domain Search；找到的邮箱送 Hunter Verifier 二次校验 |
| `backend/agents/lead_extract_agent.py` | 拿到 lead + 已知 first_name/last_name 后，调 Hunter Email Finder 构造/补全个人邮箱 |
| `backend/automation/notifier.py` | Hunter API 失败 / 余额耗尽 → 飞书告警（红色）|
| `backend/api/settings_routes.py` | 已经在白名单里，不需要改 |
| `frontend/src/routes/settings-panels.tsx:419` | 删掉那个误导性的 "企业邮箱发现" hint（因为没真用）；改成"已启用 — 用于补全企业邮箱" / "未配置" |
| `backend/requirements.txt` | 不需要加包（用 httpx 已经装好了）|

### 配额控制

- settings 加 `hunter_monthly_quota`（默认 500）—— 超过就不再调，记到飞书
- 每次调用前查本月已用 count
- 余额查询（`GET /v2/account`）每天跑一次，写到 settings 做参考

### 为什么排第 2

- 改善邮箱发现质量
- 是问题 1（waterfall）的基础——得有 3 个候选邮箱才能 waterfall
- 不阻塞 unsubscribe（3）—— 排第二

### 验收

- 1. 配 Hunter API key
- 2. 跑一个 hunt 抓取公司网站 → 日志里看到 `Hunter Domain Search: xxxxx.com → 3 emails found`
- 3. 邮箱列表里出现 Hunter 找到的、但本地正则没找到的邮箱
- 4. verifier 返回的 status 在 DB 里能看到（deliverable / risky / undeliverable）
- 5. 删 API key → 跑 hunt → 不调 Hunter，fallback 到本地正则

---

## 整体改动规模

| 类型 | 数量 |
|---|---|
| 新文件 | 3（hunter_client.py, unsubscribe.py, unsubscribe_routes.py）|
| 修改文件 | ~12 |
| 新表 | 2（email_unsubscribes, lead_email_recipients）|
| 既有表加列 | 2-3 列（lead_email_sequences.next_recipient_at, exhausted, ...）|
| 总 LOC | ~1500-2000 |
| 总时间 | 3-5 天 |

## 不在本次范围

- 退订后数据删除（GDPR Right to Erasure）—— 后续单独做
- 退订页面 UI 美化 —— 简单 HTML 即可
- 邮件 A/B 测试、追踪像素 —— 已有/后续
- 反垃圾邮件评分（SpamAssassin 集成）—— 后续

## 依赖 / 风险

| 项 | 状态 |
|---|---|
| Hunter API key | 需要用户配置，否则跳过 Hunter 步骤不影响其他功能 |
| 退订域名 | 用 api.nineluan.com 即可，不需要单独域名 |
| 数据库迁移 | 新表 + 新列，已有 `_ensure_column` helper 可平滑加列 |
| 后向兼容 | `generation_mode='personalized'` 老的依然能跑（不强制走 template）|

## 验收总目标

sprint 结束前能跑通：

```
1. 配好 SMTP + Hunter key
2. 创建一个 hunt，目标 5 家公司，每家 3 个邮箱
3. 发出去 5*3 = 15 封邮件，每封都带退订链接
4. 其中 1 封被点退订 → 该邮箱后续所有 sequence 自动跳过
5. 没回复的邮箱 3 天后自动试下一个
6. 邮件正文严格按模板生成（核心段落 + CTA 一字不差）
```

---

## 建议执行节奏

| Sprint Day | 任务 | 提交点 |
|---|---|---|
| D1 上午 | 3. unsubscribe 后端 + 邮件注入 | `feat(email): add unsubscribe link + endpoint` |
| D1 下午 | 3. 前端退订页 + scheduler 跳过 | `feat(email): skip unsubscribed addresses` |
| D2 全天 | 4. Hunter client + 接入 email_finder | `feat(tools): integrate Hunter.io email finder + verifier` |
| D3 上午 | 1. 数据模型迁移 (recipient pool) | `feat(email): multi-recipient waterfall data model` |
| D3 下午 | 1. scheduler waterfall 逻辑 | `feat(email): recipient waterfall scheduler` |
| D4 全天 | 2. 模板强约束 + 校验 | `feat(email): enforce template adherence in email_craft` |
| D5 上午 | 集成测试 + 文档 | 验收 + docs 更新 |

## 确认点

- [ ] 优先级认可（3 → 4 → 1 → 2）
- [ ] 时间预估合理
- [ ] 退订的 token 过期时间 90 天可接受
- [ ] Hunter 不接 Discover 端点可接受
- [ ] 模板强约束方式（必填 token + 重试 2 次后降级到原模板）可接受

确认后我开始 D1 上午（unsubscribe 后端）。

---

## 实施结果

| Phase | 标题 | Commit | 行数 | 测试 |
|---|---|---|---|---|
| 3 | 邮件退订 (CAN-SPAM / GDPR) | `f42e139` | +1,580 | 13/13 ✅ |
| 4 | Hunter.io 集成 (Domain Search + Email Finder + Verifier) | `91ca690` | +1,055 | 13/13 ✅ |
| 1 | 多邮箱 waterfall 序列 (3 天无回复换下一个) | `2d36abb` | +864 | 7/7 ✅ |
| 2 | 模板遵循 (required tokens + raw template fallback) | `6cdcb58` | +883 | 15/15 ✅ |

**总测试新增**: 48 个，全过；其余 781 个回归 0。

### Phase 1 关键修复
- `waiting_recipients_older_than` 改用 `sent_at` 而非 `last_attempt_at`（waterfall window 是"自发送起"，不是"自最后尝试起"）
- `int(days or 3)` 在 `days=0` 时退化到 3 的 bug 修复
- 失败路径不再 park 整个 sequence，retire 单个 recipient 后立即给下一个 pending 克隆新 message（同 pass 继续）
- `init_db` 自动迁移旧 UNIQUE index → non-unique
- `is_sequence_exhausted` 重写：仅当 pool 无 active (pending/waiting_reply) 才为 True

### Phase 2 关键决策
- 仅接 Hunter 的 3 个 v2 端点：Domain Search、Email Finder、Verifier（Discover 端点与 Brave/Tavily 重叠且贵）
- `find_emails_for_lead` 策略：本地 regex → Hunter Domain Search → Hunter Email Finder，dedup 时本地 source 优先
- 任何 Hunter 错误 → log + fallback local（best-effort，永远不阻断 lead 处理）
- Per-second 滑动限速 + 月度配额计数（新月份自动 reset）+ 429+5xx 自动重试（读 `Retry-After`）
