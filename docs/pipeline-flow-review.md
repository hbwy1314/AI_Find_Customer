# AI Hunter 任务链路 + 邮件生成链路 深度审查报告

> 版本 v1.0 | 日期 2026-09-22 | 类型 架构与流程审查（不动代码）
> 范围 `backend/` 全栈（agents / graph / automation / emailing / api）
> 方法 静态代码追踪 + 关键路径阅读 + 异常/重试/状态字段一致性扫描

---

## 0. TL;DR — 必须先看的 5 件事

| # | 严重度 | 问题 | 一句话 |
|---|---|---|---|
| 1 | 🔴 P0 | **重试机制碎片化**：全仓 grep `tenacity` / `@retry` / `stop_after_attempt` 命中数 = **0**，LLM/SMTP/Graph/HTTP 调用各自手写 try/except，重试次数、退避、分类全凭调用点自定义 | 重试靠信仰 |
| 2 | 🔴 P0 | **三处状态源不同步**：`hunt_store._hunts` 内存 dict、`data/hunts/<id>.json` 磁盘、`hunt_sessions.db` LangGraph checkpoint、`automation_queue.db` job status 四处真值，可能任意时刻不一致且无对账任务 | 重启后状态漂移 |
| 3 | 🟠 P1 | **吞异常密度高**：`api/app.py` 19 处 `except Exception`、`agents/email_craft_agent.py` 12 处、`lead_extract_agent.py` 10 处，关键路径异常被吞后状态字段被设默认值，调用方没线索 | 静默失败 |
| 4 | 🟠 P1 | **`email_craft_agent` 串行 ReAct**：2966 行单文件，每个 lead 顺序走 Think→Draft→Validate→Revise（最多 3 轮），无 LLM 缓存、无跨 lead 上下文复用，hot path 阻塞整个 hunt 的尾部 | 邮件生成是尾部 bottleneck |
| 5 | 🟡 P2 | **`reply_detector` 缺少幂等**：fetch 失败重试会重复匹配同一封 inbound，标记 reply 后可能误把同一封当两封；inbound_message_id 没有唯一索引保护 | reply 可能重复计数 |

完整问题清单与优化方案见 §3、§4。

---

## 1. 任务执行链路（Hunt Pipeline）

### 1.1 端到端流程图

```
┌──────────────────────────────────────────────────────────────────────┐
│  入口                                                                │
│  • API: POST /api/v1/hunts (api/routes.py)                            │
│  • Queue: HuntJobQueue.enqueue (automation/job_queue.py)              │
│  • CLI: hunt_queue.py worker (headless 模式)                          │
└─────────────────────┬──────────────────────────┬────────────────────┘
                      │                          │
                      ▼                          ▼
        ┌─────────────────────┐    ┌──────────────────────────┐
        │ create_hunt_internal │    │ _automation_consumer_loop │
        │ 写入 _hunts 内存 dict │    │  claim_next() 每 poll N 秒 │
        │  生成 hunt_id        │    │  requeue stale (lease)    │
        └──────────┬──────────┘    └────────────┬─────────────┘
                   │                            │
                   └─────────────┬──────────────┘
                                 ▼
              ┌──────────────────────────────────┐
              │  LangGraph StateGraph 编排        │
              │  graph/builder.py + state.py      │
              │  checkpointer = AsyncSqliteSaver  │
              └──────────────────┬───────────────┘
                                 │
   ┌─────────────────────────────┼─────────────────────────────────┐
   ▼                ▼            ▼             ▼             ▼
parse_desc    insight       keyword_gen    search        lead_extract
 (148行)     (715行)        (296行)       (364行)       (1679行)
  LLM         LLM             LLM          Jina/Google      ReAct
  1次          1次           N×round       N×kw×page       LLM + scrape
   │             │              │              │              │
   └─────────────┴──────────────┴──────────────┴──────────────┘
                                 ▼
                          evaluate (graph/evaluate.py)
                            should_continue_hunting()
                                 │
                ┌────────────────┼────────────────┐
                ▼                ▼                ▼
            "continue"      "email_craft"      "finish" → END
                │                │
       ┌────────┘                ▼
       │         email_craft_agent.email_craft_node
       │         2966行，按 leads × ReAct(max 3) 串行/并行
       │         asyncio.Semaphore(email_gen_concurrency)
       │                        │
       └────────────────────────┴──► END
```

### 1.2 各环节输入/处理/输出表

| 节点 | 文件 | 输入 | 处理 | 输出 | 异步/并行 | 重试 |
|---|---|---|---|---|---|---|
| `parse_description_node` | `agents/parse_description_agent.py` | `HuntState.description` | LLM 单次抽取 | 填充 `target_customer_profile` / `target_regions` | 同步 async | 无 |
| `insight_node` | `agents/insight_agent.py` | `HuntState` 业务字段 | LLM 分析 → ICP | `insight` dict | 同步 async | 无 |
| `keyword_gen_node` | `agents/keyword_gen_agent.py` | `insight + round_feedback` | LLM 生成 5-8 个关键词 | `keywords[]` / `used_keywords[]` | 同步 async | 无 |
| `search_node` | `agents/search_agent.py` | `keywords + target_regions` | Google CSE / Bing / 其它 + Jina 抓 page | `search_results[]` / `matched_platforms[]` | asyncio.gather | 网络层未做 |
| `lead_extract_node` | `agents/lead_extract_agent.py` | `search_results + insight` | ReAct: scrape → extract → score | `leads[]` | ReAct 内部串行；外部与 search 串行 | 无 |
| `evaluate` | `graph/evaluate.py` | `state` | 纯函数 | `continue|finish`，更新 `low_yield_rounds` | 同步 | — |
| `email_craft_node` | `agents/email_craft_agent.py` | `leads[] + insight + template_seed` | 每个 lead 顺序 ReAct 3 轮 + Hunter.io enrichment | `email_sequences[]` | per-lead 并发（Semaphore），但 ReAct 内串行 | 无 |

### 1.3 关键路径源码定位

```
graph 状态机定义         graph/state.py:21-60       HuntState TypedDict（30+ 字段）
graph 编译入口          graph/builder.py:30-87     add_node + conditional_edges
路由决策               graph/evaluate.py:130-236  should_continue_hunting()
job queue 状态机        automation/job_queue.py:37-649   HuntJobQueue class
                        status 字段: queued / running / completed / failed / cancelled
                        关键转换:
                          claim_next()         line 153  queued → running
                          mark_completed()     line 269  running → completed
                          mark_failed()        line 304  running → failed
                          requeue()            line 336  failed → queued
                          cancel()             line 371  any → cancelled
                          recover_stale_*()    line 596/627  stale running → queued
后台 consumer 调度      api/app.py:757-781      _automation_consumer_loop
                        lease sweeper          api/app.py:574-589
                        poll 间隔              settings.automation_consumer_poll_seconds
embedded job 执行       api/app.py:419-554      _run_embedded_consumer_job
                        流程: template_seed → create_hunt → wait → load_result
                              → create_campaign → start_campaign
hunt 运行时存储         api/hunt_store.py       _hunts 内存 dict + data/hunts/*.json
                        load_all_hunts()       line 177
checkpoint 存储         graph/checkpointer.py   AsyncSqliteSaver
                        db path               settings.checkpoint_db_path
```

### 1.4 状态机真值表（job × hunt）

| job.status | hunt.status (内存) | hunt JSON 文件 | checkpoint 状态 | 含义 |
|---|---|---|---|---|
| queued | 不存在 | 不存在 | 不存在 | 等待 consumer 领取 |
| running | `running` | 创建后空壳 | 存在/不存在 | consumer 在跑 |
| running (心跳) | `running` | 持续 append `rounds[]` | 持续 | 正常进度 |
| running (超时) | `running` | 上次心跳的内容 | 存在 | **stale**：下一次 lease sweep 会 requeue |
| completed | `completed` | 完整 result + email_sequences | 最终 | 完成 |
| failed | `failed` | 上一次 partial | 最终 | 终态 |
| cancelled | `cancelled` | 上一次 partial（可能含 cancel_requested 标志） | 中断 | 终态 |
| interrupted（外部） | — | — | — | 已 `mark_template_seed_failed` 等不会改 hunt status |
| reclaimed | `interrupted` (running→interrupted) | 保留 | 保留 | 启动时 `recover_interrupted_running_jobs` 处理 |

**潜在状态不一致点**：
1. `mark_failed` 写 job.status=failed，但 hunt.status 可能仍为 running（如果 langgraph 已经在跑但 consumer 误判失败）
2. lease sweep 把 job requeue 后，hunt.status 没有自动回滚到 interrupted
3. checkpoint 与 job.status 不同步（job 是业务层，checkpoint 是 LangGraph 框架层）

---

## 2. 邮件生成 + 发送 + 回复链路

### 2.1 端到端流程图

```
hunt 完成（evaluate 返回 email_craft 或 finish）
        │
        ▼
email_craft_node  (agents/email_craft_agent.py:2727)
  ├─ Hunter.io enrichment  (per-domain cache, 内存)
  ├─ asyncio.Semaphore(settings.email_gen_concurrency)
  ├─ _craft_for_lead × N   (per lead 并发)
  │    └─ ReAct: Think → Draft → Validate → Revise  (max 3)
  └─ 返回 email_sequences[] 写入 state
        │
        ▼ (hunt 完成后由 consumer 创建 campaign)
_create_email_campaign_internal  (api/email_routes.py)
  ├─ store.create_campaign
  ├─ 为每个 sequence 选 email_account  (rotation)
  └─ store.add_recipients × sequence
        │
        ▼
_start_email_campaign_internal
  └─ sequence.status: draft → active
        │
        ▼ (后台 scheduler 每 5s)
_email_scheduler_loop  (api/app.py:783)
  └─ _run_scheduler_once  (emailing/scheduler.py:219-776)
        ├─ _advance_expired_recipients  (waterfall 超时)
        ├─ claim_pending_messages_ready  (15min stale 保护)
        ├─ 业务时间窗校验 (next_business_time)
        ├─ per-account 每日限额
        ├─ per-account 每小时限额
        ├─ reserve_send_quota
        ├─ send_email()  (emailing/email_sender.py)
        │    └─ Graph API: POST /me/sendMail
        │       401/403 → auth_error（永久）
        │       5xx / network → transient（10min 后重试）
        └─ 失败: mark_message_failed (retry_count++, retry_at)
                或: mark_recipient_skipped (终止)

        │ (后台 reply loop 每 N 秒)
        ▼
_email_reply_loop  (api/app.py:809)
  └─ run_graph_reply_detection_once  (emailing/reply_detector.py:330)
        ├─ graph_client.fetch_graph_replies  (Graph: GET /me/messages?$filter)
        ├─ process_inbound_messages
        │    ├─ _match_sent_message  (message-id / in-reply-to / references / conversation / subject+from)
        │    ├─ 写入 email_replies 表
        │    └─ 更新 sequence.status = replied / stopped
        └─ _maybe_notify_reply_matches  (飞书通知)
```

### 2.2 各环节输入/处理/输出表

| 节点 | 文件 | 输入 | 处理 | 输出 | 并发 | 重试 |
|---|---|---|---|---|---|---|
| Hunter.io enrichment | `email_craft_agent.py:138` | `domain` | HTTP GET Hunter | `contacts[]` | domain cache 命中复用 | **无**（API 配额贵） |
| `_craft_for_lead` | `email_craft_agent.py` (内) | `lead + insight + template_seed` | ReAct 3 轮 LLM 调用 | 单条 email_sequence | 外部 Semaphore | **无** |
| `_personalize_template_sequence` | `email_craft_agent.py:1240` | `template + lead` | 模板变量替换 + 校验 token | sequences[] | 串行 | 无 |
| `send_email` | `emailing/email_sender.py:99` | `account + payload` | Graph `sendMail` + `reserve_send_quota` | `provider_message_id` | 调用方控 | 区分 auth/permanent/transient |
| Scheduler 单 pass | `emailing/scheduler.py:219` | DB 中 pending messages | 业务窗 + 限额 + 抢配额 | sent/failed/skipped | 单 pass 串行 | 自动调度重试（DB 状态） |
| Reply fetch | `emailing/graph_client.fetch_graph_replies` | `account + recent_days` | Graph `GET /me/messages` | inbound[] | 单 mailbox 串行 | **无** |
| Reply match | `reply_detector._match_sent_message:393` | `inbound` | 5 级匹配（message-id / in-reply-to / refs / conversation / subject+from） | `sent_message` dict | 串行 | — |

### 2.3 发送失败分类（关键）

| 错误 | 来源 | 当前分类 | 行为 |
|---|---|---|---|
| HTTP 401 | Graph API | `auth_error` | 永久失败，message → failed |
| HTTP 403 | Graph API | `auth_error` | 同上 |
| HTTP 429 | Graph API | **未明确分类**（默认归 permanent_failure?） | 可能被永久放弃 |
| HTTP 5xx | Graph API | `transient` | 10min 后重试 |
| 网络断 | aiohttp | `transient` | 10min 后重试 |
| 收件人拒收 | Graph API status 4xx | `permanent_failure` | 永久失败 |
| `retry-after` 头 | Graph API | **未解析** | 没用上官方建议的退避时间 |
| 配额耗尽 (402/quota) | Graph API | **未识别** | 可能误判永久 |

`emailing/scheduler.py:331` 的 `retry_at = current_dt + timedelta(minutes=10)` 是**固定 10 分钟**，无指数退避、无 jitter、无 `retry-after` 解析。

---

## 3. 问题清单与优化方案

### 3.1 🔴 P0-1 重试机制碎片化

**现象**：全仓 grep `tenacity` / `@retry` / `stop_after_attempt` 命中数 = **0**。LLM / SMTP / Graph / HTTP 失败全靠各自手写 try/except，重试策略零散。

**影响**：
- LLM 调用失败直接传播到 hunt → job failed → 用户看不到结果，可能重试 1 次就放弃
- Graph API 限流时未走 `retry-after`，等于硬扛
- SMTP 临时错误直接判永久，浪费 lead

**优化项（落地）**：

| 优化 | 工作量 | 收益 | 文件建议 |
|---|---|---|---|
| 引入 `tenacity` 库 | 0.5d | 统一重试抽象 | `requirements.txt` + 新建 `util/retry.py` |
| 写 `with_retry` 装饰器，按异常类型分桶（LLMTrans / NetTrans / AuthPerm） | 1d | 一处定义，全栈复用 | `util/retry.py` |
| LLM 调用全部走 `with_retry`，3 次指数退避（1s/4s/16s + jitter） | 1d | LLM 抖动场景成功率提升 30%+ | `agents/*.py` |
| Graph API 解析 `Retry-After` header + 429 → transient | 0.5d | 不再硬扛限流 | `emailing/graph_client.py` |
| 修 SMTP transient → 走 message.retry_count++ 而非 failed | 0.5d | 节省 lead 配额 | `emailing/email_sender.py` |

### 3.2 🔴 P0-2 三处状态源不同步

**现象**：`hunt_store._hunts`（内存 dict）、`data/hunts/<id>.json`（磁盘）、`hunt_sessions.db`（LangGraph checkpoint）、`automation_queue.db`（job status）四处真值，无对账任务。

**影响**：
- 重启后从 JSON 恢复，可能与 checkpoint 不同步 → 部分 lead 被忽略
- job status=failed 但 hunt 内存里仍是 running → SSE 给前端发错状态
- lease sweep 后 hunt 不知道

**优化项（落地）**：

| 优化 | 工作量 | 收益 |
|---|---|---|
| 在 `emailing/store.py` / `hunt_store` / `job_queue` 引入 **统一的 event log 表**（append-only），所有状态变更先写 event，再写字段 | 2d | 单一真值源 + 可重放 |
| 后台对账任务：每 5min 扫 `job.status=running` 但 `hunt.status≠running` 的，标记 `inconsistent=true` 并触发 reconcile | 1d | 自动发现漂移 |
| `load_all_hunts` 时如发现 checkpoint.thread_id 无对应 hunt 文件 → 删除孤儿 checkpoint | 0.5d | `_cleanup_orphan_checkpoints` 已存在（api/app.py:137），加强覆盖 |
| hunt JSON 与 checkpoint 写顺序：先 JSON fsync，再写 checkpoint；崩溃恢复以 JSON 为准 | 0.5d | 避免恢复后少数据 |

### 3.3 🟠 P1-1 吞异常密度高

**现象**（grep 统计）：
- `api/app.py`: **19** 处 `except Exception`
- `agents/email_craft_agent.py`: **12** 处
- `agents/lead_extract_agent.py`: **10** 处
- `emailing/graph_client.py`: **7** 处
- `agents/insight_agent.py`: **7** 处

**影响**：调用方拿不到真实错误，监控拿不到告警信号，用户看到"成功"但实际是 fallback。

**优化项（落地）**：

| 优化 | 工作量 | 收益 |
|---|---|---|
| 区分 4 类异常处理策略：`(a) 静默吞 + DEBUG 日志` `(b) 静默吞 + WARN 日志 + 计数器` `(c) 重抛` `(d) 触发告警`。每处加注释注明属于哪一类 | 1d | 文档化意图 |
| 引入 `metrics.counter("exception_swallowed", tags=[file, lineno])`，吞异常必打点 | 0.5d | 可观测性 |
| 关键路径（send_email / fetch_replies / claim_next）禁止裸 `except Exception`，必须列出具体异常 | 1d | 不再掩盖 TypeError |

### 3.4 🟠 P1-2 email_craft 串行 ReAct，hot path 阻塞

**现象**：`agents/email_craft_agent.py:2727` `email_craft_node` 每个 lead 走 ReAct（Think → Draft → Validate → Revise，max 3 轮），单 lead 平均 6-9 次 LLM 调用。默认 `email_gen_concurrency=4`，100 leads × 9 次 LLM ÷ 4 ≈ 225 次串行调用，**单 hunt 邮件生成阶段 30-60min**。

**影响**：100 leads 的 hunt，邮件阶段比 hunt 本身（search+extract）还慢。

**优化项（落地）**：

| 优化 | 工作量 | 收益 |
|---|---|---|
| **模板+动态填充模式**（template_seed 已具备）：高价值 lead 才走 ReAct，低置信度走模板填充（`_personalize_template_sequence` 已有，可扩大使用率） | 2d | 邮件阶段耗时减半 |
| **LLM 响应缓存**：相同 (lead.industry, lead.country_code, template_id) → 复用上次结果（Redis 或 SQLite + LRU） | 1d | 重复行业不重复调 LLM |
| **跨 lead 批量 prompt**：把 5-10 个 lead 合并成一次 LLM 调用（每 lead 返回独立 result），用 structured output | 2d | LLM 调用数 ÷5~10 |
| `email_gen_concurrency` 默认从 4 提到 8-16（看 LLM 配额），并支持 per-tenant 限速 | 0.5d | 单机吞吐 ×2-4 |

### 3.5 🟠 P1-3 job 重试退避缺失

**现象**：`automation/job_queue.py:336` `requeue()` 直接 `status=queued`，没有指数退避，stale 后下次可能立刻又失败。

**影响**：上游 LLM 暂时性故障（429 / 5xx）会连续失败 → 用户看到 job 一直重试。

**优化项（落地）**：

| 优化 | 工作量 | 收益 |
|---|---|---|
| `requeue()` 加 `next_attempt_at` 字段：失败 N 次 → 退避 `2^N` 分钟 + jitter（最多 60min） | 1d | 不再立即重试 |
| 失败 ≥ 5 次 → 自动切到 `dlq` 表，人工介入 | 0.5d | 不再无限重试 |
| `claim_next()` 只拉 `next_attempt_at <= now()` 的 job | 0.5d | 配合退避 |

### 3.6 🟠 P1-4 scheduler 限流逻辑

**现象**：`emailing/scheduler.py` 只有 per-account 每日上限 + 每小时上限，**没有抖动**、**没有 retry-after 解析**、**没有跨账号 round-robin**。

**优化项**：

| 优化 | 工作量 | 收益 |
|---|---|---|
| Scheduler 发送前加 0-30s 随机 jitter（防止 burst） | 0.5d | 降低被识别为机器发送 |
| 解析 Graph 返回的 `Retry-After` 头 → 写入 message.retry_at | 0.5d | 限流时不超发 |
| 跨账号 round-robin：每个 sequence 的 50 个收件人分散到 3-5 个邮箱，避免单账号 burst | 1d | 提升送达率 |

### 3.7 🟡 P2-1 reply_detector 幂等缺失

**现象**：`run_graph_reply_detection_once` 失败重试时，Graph 可能返回同一封 message 两次；`_match_sent_message` 没保护，可能同一封 inbound 被写入 `email_replies` 表两次。

**影响**：reply 计数偏高，影响 campaign summary 与模板评分。

**优化项**：

| 优化 | 工作量 | 收益 |
|---|---|---|
| `email_replies` 表加 unique 约束 `(inbound_message_id)` + INSERT OR IGNORE | 0.5d | 数据库层防重复 |
| fetch_graph_replies 用 `delta_token` 增量（Outlook `X-AnchorMailbox` + `$delta`） | 2d | 不再轮询 14 天 |
| 失败重试带指数退避（见 P0-1） | 0 | 复用 |

### 3.8 🟡 P2-2 insight_agent 失败 → 整 hunt 不可恢复

**现象**：insight 是单点 LLM，失败则 hunt 直接挂掉。但 insight 本质是行业/产品分析，可以降级。

**优化项**：

| 优化 | 工作量 | 收益 |
|---|---|---|
| insight LLM 失败 → 用 keyword-only baseline（基于 description + product_keywords 拼 insight） | 1d | 不再因为 insight 失败拖死 hunt |
| insight 落库可缓存：相同 `(industry, target_customer_profile)` → 复用 7 天内结果 | 1d | 行业 hunt 提速 |

### 3.9 🟡 P2-3 search_agent 没有 dedup 跨轮

**现象**：`agents/search_agent.py` 的 `seen_urls` 在 LangGraph state 里，但每轮 search 是单独跑；同样关键词可能在不同 round 被复用。

**影响**：浪费 Google CSE 配额。

**优化项**：

| 优化 | 工作量 | 收益 |
|---|---|---|
| `seen_urls` 与 `used_keywords` 在 keyword_gen_node 入口强制去重 | 0.5d | 不重复搜 |

### 3.10 🟡 P2-4 监控与可观测性短板

**现象**：
- 关键路径（send_email / fetch_replies / claim_next）缺少指标埋点
- Langfuse 已接入但只在 insight / keyword / lead_extract 等 LLM 节点，**scheduler / reply_detector / consumer loop 没有 trace**
- 没有 SLO（如 p95 hunt 时长、p95 邮件生成时长）

**优化项**：

| 优化 | 工作量 | 收益 |
|---|---|---|
| 在 consumer / scheduler / reply loop 入口加 `metrics.histogram("loop.duration_ms")` + 失败计数器 | 1d | 可监控 |
| Langfuse 扩展到非 LLM 节点（用 span 标注） | 1d | 全链路可追踪 |
| 定义 SLO：`hunt.p95 < 15min`, `email_gen.p95 < 10min`, `reply.p95 fetch < 30s` | 0.5d | 量化目标 |

### 3.11 🟢 P3 流程简化（可选）

1. **统一 progress 通道**：现在 progress 同时走 SSE + DB + log，三处重复，建议 SSE 为单一推送源，DB 是快照
2. **取消 hunt** 的链路：consumer 检测到 cancel → 中断 LangGraph invoke，目前没有显式 cancel token 注入，需要看 `HuntCancelledError` 触发路径是否真的能终止 graph
3. **template_seed 流程**：当前在 consumer 入口和 hunt 创建前都会算一次，浪费 LLM 配额——需要确认是否有 dedup（见 `mark_template_seed_preparing` job_queue.py:515）

---

## 4. 优化项汇总表（按 ROI 排序）

| 优先级 | 优化项 | 工作量 | 影响范围 | 建议窗口 |
|---|---|---|---|---|
| 🔴 P0-1 | 引入 tenacity 统一重试 + 解析 Retry-After | 3d | 全栈 | 本周 |
| 🔴 P0-2 | 统一 event log + 状态对账任务 | 3d | hunt/email 状态机 | 本周 |
| 🟠 P1-3 | job 重试退避 + DLQ | 1.5d | automation | 下周 |
| 🟠 P1-4 | scheduler jitter + Retry-After 解析 | 1d | email 发送 | 下周 |
| 🟠 P1-2 | email_craft 模板降级 + LLM 缓存 | 3d | email 生成 | 下周 |
| 🟠 P1-1 | 异常分类与监控埋点 | 2.5d | 全栈 | 下下周 |
| 🟡 P2-7 | reply 幂等 + delta 增量 | 2.5d | reply loop | 下下周 |
| 🟡 P2-8 | insight 降级 + 缓存 | 2d | hunt 入口 | 月底 |
| 🟡 P2-4 | 全链路监控 + SLO | 2.5d | 可观测性 | 月底 |

---

## 5. 已验证结论（无需代码改动即可采纳的）

1. ✅ hunt pipeline 主链路 LangGraph 编排正确，evaluate 三段 stop condition 合理
2. ✅ reply_detector 五级匹配（message-id / in-reply-to / refs / conversation / subject+from）健壮
3. ✅ scheduler 业务时间窗 + 限额 + 多账号 fallback 设计良好
4. ✅ lease sweeper 解决了 SIGKILL 后的 job 卡死
5. ✅ template_seed 与 hunt 创建解耦是好的（重试不互相影响）

## 6. 待澄清问题（需用户决定）

1. **`email_craft_node` 是否允许模板降级到非 ReAct 模式**？会牺牲个性化质量换性能
2. **是否引入 Redis 做 LLM 响应缓存**？基础设施成本
3. **DLQ 失败后人工介入的 UI 是否要做**？给运维加一个 `/api/v1/admin/dlq` 接口
4. **`automation_consumer_poll_seconds` 默认 60s 能否缩到 5-10s**？取决于 SQLite 写盘压力
5. **Graph API `retry-after` 在多租户场景下要不要 per-tenant 隔离限速**？

---

## 7. 验证方式建议（如何证明优化生效）

1. **重试**：mock LLM 500 错误 3 次后第 4 次成功，验证 hunt.status=completed 而非 failed
3. **退避**：mock 故障后看 `job.requeue_count` 与 `next_attempt_at` 时间序列
4. **状态对账**：故意制造不一致（kill -9 进程），启动后 5min 内 reconcile 完成
5. **邮件生成耗时**：100 leads hunt 从 60min → 20min 以下
6. **reply 幂等**：mock Graph 重复返回同一 messageId，DB 只插 1 条

---

> 报告结束。如需针对某条优化项出实施计划（_plan.md + diff），请指明优先级序号。