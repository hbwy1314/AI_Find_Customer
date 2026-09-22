# AI Hunter 整体优化方案

> 版本：v1.0 | 日期：2026-09-22 | 状态：待审批
> 前置结论：代码缺陷层面经多轮修复已扎实（992 测试全绿）；本方案聚焦架构与工程化，系统性消除整类问题而非逐个打补丁。

---

## 目标与原则

**核心目标**：消除多进程共享状态靠 JSON 文件协调这一架构瓶颈，同时补齐依赖复现、结构拆分、可观测性四块短板。

**原则**：
1. 每阶段独立可发布、可回滚，不搞一次性大重构
2. 迁移期间 JSON 与 SQLite 双写双读可切换（feature flag），验证后再切换读路径
3. 全程保持测试全绿——现有 992 个测试是最重要的安全网

---

## 阶段总览与依赖关系

```
P0 前置准备 ──┬──> P1 数据层迁移(核心) ──> P2 无状态 Web 层
              │                              │
              └──> P3 结构重构 ──────────────┤
                                             v
                              P4 可靠性增强 ──> P5 容器化(可选)
```

| 阶段 | 内容 | 预估工作量 | 风险 |
|------|------|-----------|------|
| P0 | 依赖锁定 + CI 冒烟 + schema 版本机制 | 1 天 | 低 |
| P1 | Hunt JSON → SQLite 单一事实源 | 3-5 天 | 中 |
| P2 | Web 层无状态化 + 跨进程事件通知 | 2-3 天 | 中 |
| P3 | 巨石文件拆分（前后端） | 3-4 天 | 低 |
| P4 | Graph Webhook + Scheduler 索引 + Metrics | 2-3 天 | 中 |
| P5 | Docker 多阶段构建 | 1 天 | 低 |

---

## P0：前置准备（地基）

### P0.1 依赖锁定
- **现状**：`requirements.txt` 全宽范围（`>=x,<1.0`），构建不可复现
- **方案**：引入 `uv`，生成 `requirements.lock`；CI 改用 lock 安装；`pyproject.toml` 保持声明式依赖
- 验收：两次全新环境安装产出 hash 一致

### P0.2 CI 后端启动冒烟
- **现状**：CI 只有 ruff + pytest，无"服务能起来"检查（FastAPI lifespan、路由注册类回归无法拦截）
- **方案**：新增 CI step——`uvicorn api.main:app` 后台启动 + 轮询 `/health` 至 200 + 调用一个只读端点 + 优雅退出
- 验收：故意引入路由注册错误时 CI 能红

### P0.3 Schema 版本机制（为 P1 铺路）
- **现状**：`emailing/store.py` 15+ 张表全是 `CREATE TABLE IF NOT EXISTS`，`scripts/` 下 16 个一次性修复脚本就是代价
- **方案**：
  - SQLite 用 `PRAGMA user_version` 存版本号
  - 建 `migrations/` 目录：`001_xxx.sql` 按序执行，记录到 `_migrations` 表
  - 新表 DDL 只写在 migration 里，`store.py` 删除内联 CREATE TABLE
- 验收：空库从零建全量 schema；旧库（user_version=0）自动升级

---

## P1：Hunt 数据层迁移（核心阶段）

### 现状与问题
- `api/hunt_store.py`：JSON 文件 + `fcntl` 文件锁 + 原子替换；`backend/data/` 12MB / 86 个文件
- `api/routes.py`：71 处直接引用 `_hunts` 内存缓存，配套 `_get_hunt` 惰性回载 + `_evict_excess_hunts` 淘汰——全是"缓存与事实源不一致"的补丁
- Web 进程与 `headless_worker` 进程各持一份内存，SSE 事件只在 Web 进程内有效

### 方案设计

**新表结构**（沿用 `emailing/store.py` 的 WAL + busy_timeout 模式）：

```sql
CREATE TABLE hunts (
    hunt_id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL,
    status TEXT NOT NULL,              -- queued/running/done/failed/cancelled
    title TEXT,
    params_json TEXT NOT NULL,        -- 输入参数（ICP、目标市场等）
    result_json TEXT,                 -- leads 等大结果
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX idx_hunts_owner_status ON hunts(owner_user_id, status);
CREATE INDEX idx_hunts_updated ON hunts(updated_at DESC);
```

**实施步骤**（按提交粒度，每步可独立回滚）：

1. **写路径双写**：`hunt_store.py` 新增 `save_hunt_db()`，在现有 `save_hunt()` 内同时写 JSON 与 SQLite（SQLite 失败仅告警不阻断，JSON 仍为权威）
2. **一次性回填**：迁移命令 `scripts/migrate_hunts_to_sqlite.py`——遍历 `backend/data/*.json` 入库（跳过已存在 hunt_id），输出对账报告（数量、抽样 diff）
3. **读路径切换**：`_get_hunt()` 改为 SQLite 直读 + 进程内短 TTL 缓存（仅 30s，替代现在的全量常驻内存）。71 处调用点无需改动——这是选择从 `hunt_store.py`/`_get_hunt` 接缝切入的原因
4. **跨进程 dedup 迁移**：`current_lead_keys()`/`accept_new_leads()` 的文件锁逻辑改为 SQLite 事务（`BEGIN IMMEDIATE`），删除 `.dedup.lock`
5. **删除 JSON 权威**：确认稳定后（建议观察 1 周生产），JSON 改为仅归档导出（导出端点保留）

**性能要点**：`result_json` 可达数 MB，列表页查询不 SELECT result_json（列表接口只取元数据列）；详情页按主键单查。

### 风险与回滚
- 每步 feature flag（`settings.hunt_storage_backend = "json" | "sqlite" | "dual"`），任一步出问题切回 json 即回滚
- 最大风险：步骤 3 的读缓存一致性——靠 30s TTL + 写后主动失效（写路径更新后清缓存）双保险

### 验收标准
- 全量测试绿（`test_routes.py` 38 处 hunt 相关测试不改动应通过——证明接口兼容）
- 并发测试：Web 进程读、worker 进程写同一 hunt，无脏读
- `scripts/` 下 4 个 dedupe 脚本逻辑收编进 `accept_new_leads()` 后删除

---

## P2：Web 层无状态化 + 跨进程事件通知

**前置依赖：P1 完成**

### P2.1 SSE 事件统一走 DB
- **现状**：`api/sse.py` 依赖 Web 进程内 `_sse_queues`；worker 进程的进度（automation job、scheduler 发送结果）无法实时推给浏览器，前端靠轮询兜底
- **方案**：复用 `automation/job_queue.py` 的 SQLite 队列模式，新增 `hunt_events` 表（event_id 自增, hunt_id, event_type, payload_json, created_at）
  - 写端：graph 流水线各节点完成时写事件（替代现在往进程内 queue 塞）
  - 读端：Web 进程 `sse.py` 起一个后台任务，按自增 cursor 轮询新事件分发到连接（间隔 1s，对浏览器仍是实时推送）
- 验收：headless_worker 跑 hunt 时，浏览器 SSE 能实时收到阶段事件

### P2.2 内存缓存退役
- P1 完成后 `_hunts`/`_evict_excess_hunts` 整体删除
- `app.py` 启动时不再全量加载 hunt 文件（当前启动要读 86 个 JSON）

---

## P3：巨石文件拆分（与 P1/P2 可并行）

### 后端
| 文件 | 行数 | 拆分方案 |
|------|------|---------|
| `api/routes.py` | 2237 | 按域拆：`hunt_routes.py`（CRUD）、`hunt_run_routes.py`（启动/停止/继续挖掘）、`export_routes.py`；`app.py` 里 include 顺序不变 |
| `emailing/store.py` | 2467 | 按表域拆：`store/accounts.py`、`store/campaigns.py`、`store/messages.py`、`store/schema.py`（P0.3 的 migration 入口） |
| `agents/email_craft_agent.py` | 2806 | 拆 prompt 构造 / LLM 调用 / 结果解析三层；prompt 移入 `prompts/` 目录 |

**纪律**：纯移动不改逻辑，每拆一个文件跑一次全量测试。

### 前端
| 文件 | 行数 | 拆分方案 |
|------|------|---------|
| `routes/hunt-detail.tsx` | 3483 | 按面板拆 `detail/` 目录：`LeadsPanel`、`EmailDraftPanel`、`ProgressTimeline`、`HuntSettings` 等；已有 React Query 直接复用 |
| `api/client.ts` | 1157 | 按域拆 `api/hunts.ts`、`api/emails.ts`、`api/automation.ts`、`api/settings.ts` |

---

## P4：可靠性增强

### P4.1 Graph 回复检测：轮询 → Webhook
- **现状**：`reply_detector` 每 30s 全量拉收件箱，`graph_client.fetch_graph_inbox_messages` 分页拉取（已修复 nextLink 问题），延迟 + API 配额双成本
- **方案**：
  - 主路径：MS Graph change notifications（subscription），回调端点 `/api/graph/webhook` 校验 validationToken 后入队
  - 兜底：轮询保留但降频至 15 分钟（处理漏推）
  - 断线恢复：用 delta query 增量同步替代全量
- 验收：回复从到达到检测 < 10s；轮询 API 调用下降 > 90%

### P4.2 Scheduler 到期索引
- **现状**：每个 pass 全表扫描 pending sequences
- **方案**：`lead_email_sequences` 加 `next_scheduled_at` 索引列，查询改 `WHERE status='pending' AND next_scheduled_at <= now`（万级以下无感，但为扩展铺路）

### P4.3 Prometheus Metrics
- `observability/` 新增 `/metrics` 端点：发送成功率、账号轮换/封禁计数、序列状态分布、LLM 调用时长/成本（langfuse 之外的基础设施层指标）

---

## P5：容器化（可选，独立决策）

- 多阶段 Dockerfile：`node:20` build 前端 → `python:3.12-slim` 装依赖 + 拷 dist，静态文件由 FastAPI `StaticFiles` 挂载
- `docker-compose.yml`：单容器（SQLite 本地卷挂载），与现有 systemd/nginx 路径并存不冲突
- 注意：文件锁（fcntl）在容器内单进程模式无意义，P1 完成后自然消除

---

## 不做的事

| 项 | 理由 |
|----|------|
| 迁 PostgreSQL | SQLite WAL 在单机自部署场景足够；需求出现时 P1 的表结构可直接平移 |
| 拆微服务 | 单机自部署产品，进程内模块化 + headless_worker 已是最优拓扑 |
| 重写前端状态层 | React Query + Router 选型正确，只需拆文件 |
| 强类型 ORM（SQLAlchemy） | 手写 SQL + migration 已满足规模，引入 ORM 收益低于学习/迁移成本 |

---

## 实施节奏建议

```
第 1 周   P0（全量）+ P1 步骤 1-2（双写 + 回填）
第 2 周   P1 步骤 3-5（读切换 + dedup 迁移 + 观察）
第 3 周   P2（SSE 事件化 + 缓存退役）∥ P3 后端拆分（并行）
第 4 周   P3 前端拆分 + P4.2/P4.3
第 5 周   P4.1（Webhook，需租户管理员权限配置，预留调试时间）+ P5（可选）
```

**每个阶段的完成定义（DoD）**：测试全绿 + CI 冒烟通过 + 手动回归一次主链路（创建 hunt → 挖掘 → 邮件草稿 → campaign 发送 → 回复检测）。
