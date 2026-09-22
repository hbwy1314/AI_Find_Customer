# P1 — Hunt JSON → SQLite 迁移方案

**作者**：P8 AI 编程专家
**日期**：2026-09-22
**状态**：待用户决策
**前置**：P0 已完成（uv + CI 冒烟 + migrations 引擎）

---

## 1. 目标与背景

### 1.1 当前架构（要替换的）

```
backend/data/hunts/{hunt_id}.json  ← 单文件存储（20+ 文件）
        │
        ▼
hunt_store.py （封装 fcntl 文件锁 + atomic JSON write）
        │
        ▼
api/routes.py / app.py / 9 个调用方
```

**已知问题**：
| 问题 | 影响 |
|---|---|
| fcntl 跨进程锁弱 | 多 worker uvicorn 启动时偶发 `BlockingIOError` |
| JSON 嵌套 leads 无索引 | dedup/统计/查询要全文件 load + parse |
| JSON 嵌套 cost_summary 无历史 | LLM 成本分析只能聚合到内存 |
| hunt_jobs（SQLite）与 hunt JSON 跨库 | JOIN 必须经 UUID 字符串比较 |
| `purge_old_hunts` 走 glob + rm | 无事务保护，删除途中崩了不一致 |
| `accept_new_leads` / `_dedup_hunt_paths` 全内存 | hunt 量大时 OOM 风险 |

### 1.2 目标架构（要实现的）

```
backend/data/hunts.db
  ├── hunts        (hunt_id PK + 18 顶层字段)
  ├── hunt_leads   (lead_id PK + hunt_id FK + 8 lead 字段)
  ├── hunt_stage_snapshots  (snapshot_id PK + hunt_id FK + stage/state JSON)
  ├── hunt_cost_events      (event_id PK + hunt_id FK + provider/model/cost)
  └── hunt_locks            (hunt_id PK + holder + acquired_at + mode) ← 取代 fcntl
```

**收益**：
- WAL 模式多 reader + 1 writer（解决 fcntl 锁竞争）
- SQL JOIN / WHERE / INDEX（dedup / 统计 / 历史成本分析）
- hunt_jobs ↔ hunts 同库 JOIN
- `purge_old_hunts` 一条 `DELETE FROM hunts WHERE created_at < ?`
- `accept_new_leads` 用 `INSERT OR IGNORE`（PK = hunt_id+lead_key）
- 文件锁改 SQLite `hunt_locks` 表（事务内 acquire，崩了自动释放）

---

## 2. 数据模型（SQLite DDL）

### 2.1 `hunts` 主表

```sql
-- 002_create_hunts_table.sql
CREATE TABLE hunts (
    hunt_id              TEXT PRIMARY KEY,
    status               TEXT NOT NULL DEFAULT 'pending',
    current_stage        TEXT NOT NULL DEFAULT '',
    hunt_round           INTEGER NOT NULL DEFAULT 0,
    leads_count          INTEGER NOT NULL DEFAULT 0,
    email_sequences_count INTEGER NOT NULL DEFAULT 0,
    result               TEXT NOT NULL DEFAULT '',
    error                TEXT NOT NULL DEFAULT '',
    website_url          TEXT NOT NULL DEFAULT '',
    product_keywords     TEXT NOT NULL DEFAULT '',   -- JSON list
    target_customer_profile TEXT NOT NULL DEFAULT '', -- JSON obj
    target_regions       TEXT NOT NULL DEFAULT '',   -- JSON list
    email_template_examples TEXT NOT NULL DEFAULT '', -- JSON list
    email_template_notes TEXT NOT NULL DEFAULT '',
    created_at           TEXT NOT NULL,
    completed_at         TEXT NOT NULL DEFAULT '',
    updated_at           TEXT NOT NULL DEFAULT (datetime('now')),
    cost_summary         TEXT NOT NULL DEFAULT ''    -- JSON obj (denormalized fast view)
);

CREATE INDEX idx_hunts_status_created ON hunts(status, created_at DESC);
CREATE INDEX idx_hunts_completed_at   ON hunts(completed_at) WHERE status = 'done';
CREATE INDEX idx_hunts_website        ON hunts(website_url) WHERE website_url != '';
```

### 2.2 `hunt_leads` 子表（拆嵌套 leads）

```sql
-- 003_create_hunt_leads_table.sql
CREATE TABLE hunt_leads (
    lead_id        TEXT PRIMARY KEY,
    hunt_id        TEXT NOT NULL REFERENCES hunts(hunt_id) ON DELETE CASCADE,
    lead_key       TEXT NOT NULL,         -- identity key (domain+company norm)
    identity_json  TEXT NOT NULL DEFAULT '{}', -- 完整 lead dict（保留灵活 schema）
    status         TEXT NOT NULL DEFAULT 'new',
    round_introduced INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    UNIQUE(hunt_id, lead_key)
);

CREATE INDEX idx_hunt_leads_hunt_id  ON hunt_leads(hunt_id);
CREATE INDEX idx_hunt_leads_status   ON hunt_leads(hunt_id, status);
CREATE INDEX idx_hunt_leads_key      ON hunt_leads(lead_key);  -- 跨 hunt 去重
```

### 2.3 `hunt_stage_snapshots` 历史表

```sql
-- 004_create_hunt_stage_snapshots.sql
CREATE TABLE hunt_stage_snapshots (
    snapshot_id  TEXT PRIMARY KEY,
    hunt_id      TEXT NOT NULL REFERENCES hunts(hunt_id) ON DELETE CASCADE,
    stage        TEXT NOT NULL,
    state_json   TEXT NOT NULL DEFAULT '{}',
    captured_at  TEXT NOT NULL
);

CREATE INDEX idx_snapshots_hunt_time ON hunt_stage_snapshots(hunt_id, captured_at DESC);
```

### 2.4 `hunt_cost_events` 成本明细

```sql
-- 005_create_hunt_cost_events.sql
CREATE TABLE hunt_cost_events (
    event_id    TEXT PRIMARY KEY,
    hunt_id     TEXT NOT NULL REFERENCES hunts(hunt_id) ON DELETE CASCADE,
    provider    TEXT NOT NULL,         -- openai / anthropic / tavily / ...
    model       TEXT NOT NULL DEFAULT '',
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd    REAL NOT NULL DEFAULT 0.0,
    captured_at TEXT NOT NULL
);

CREATE INDEX idx_cost_hunt_time ON hunt_cost_events(hunt_id, captured_at DESC);
CREATE INDEX idx_cost_provider ON hunt_cost_events(provider, captured_at DESC);
```

### 2.5 `hunt_locks` 取代 fcntl

```sql
-- 006_create_hunt_locks.sql
CREATE TABLE hunt_locks (
    hunt_id       TEXT PRIMARY KEY,
    holder        TEXT NOT NULL,        -- worker_id / pid
    mode          TEXT NOT NULL DEFAULT 'write',  -- write | read
    acquired_at   TEXT NOT NULL,
    expires_at    TEXT NOT NULL          -- 防止 worker 崩了不释放
);

CREATE INDEX idx_locks_expires ON hunt_locks(expires_at);
```

---

## 3. 迁移路径（4 Wave）

### Wave 1 — 加表 + 双写（10 月 1 日前完成）

**目标**：schemas 落地，**所有写入路径同时写 JSON 和 SQLite**，读仍走 JSON（确保 SQLite 数据真实性不被怀疑）

```
1. 写 002-006 migrations/*.sql 文件
2. 写 backend/migrations/applied 验证（uv run pytest tests/test_migrations.py 仍 8 通过）
3. 重构 hunt_store.py：
   - 拆出 SQLiteBackend + JSONBackend 两个类
   - 共用 HuntBackendStorage ABC（load/save/list/delete/lock/...）
   - 写路径调双 backend；读路径默认走 JSON（向后兼容）
4. 加 new tests/test_hunt_store_sqlite.py：
   - 单测 SQLiteBackend 全部 12 个方法
   - 单测 ABC 双写一致性（write-through 到两边 + 立刻读 SQLite 一致）
5. 跑全套 pytest 必须 1000+8 通过
6. commit: "P1.1: SQLite schema + hunt_store dual-write backend"
```

**风险**：
- 双写不一致（一边失败）→ 加 try/except：JSON 成功 SQLite 失败时**记 warning 日志**，下次启动时 SQLite 回填脚本自动补齐
- _write_json_atomic vs SQLite 事务的语义差异（SQLite 是 WAL atomic commit，JSON 是 temp+rename atomic）→ 测试覆盖

**回滚**：`git revert` 即可，因为读仍走 JSON

### Wave 2 — 回填脚本 + 验证（10 月 8 日前完成）

**目标**：把现有 20+ JSON 文件一次性导入 SQLite，并验证**逐字段一致**

```
1. 写 backend/scripts/migrate_hunts_to_sqlite.py：
   - 遍历 backend/data/hunts/*.json
   - 解析 → 写 hunts + hunt_leads + hunt_stage_snapshots + hunt_cost_events
   - 校验：每行读 SQLite 与 JSON 字段比较，差异写入 scripts/data/migrate_hunts_audit.csv
2. dry-run 模式默认开启：先 dry-run 跑一遍，看 audit.csv
3. 跑一次确认 audit.csv 是空表（一致）
4. 加 tests/test_migrate_hunts.py：
   - 给定 N 个 mock JSON，dry-run 报告条数
   - apply 模式后 SQLite 表行数与 JSON 数匹配
5. commit: "P1.2: hunt JSON → SQLite migration script + tests"
```

**回滚**：脚本不动数据，只增；可重跑（idempotent 用 INSERT OR IGNORE）

### Wave 3 — 读切换（10 月 15 日前完成）

**目标**：读路径从 JSON 切换到 SQLite，**JSON 仍写但不被读**

```
1. hunt_store.py 加 FEATURE_FLAG = config.settings.HUNT_SQLITE_READ_ENABLED（默认 false）
2. 改所有读方法（load_hunt / list_hunts / current_leads / accept_new_leads / ...）：
   - if flag: SQLiteBackend.read()
   - else: JSONBackend.read() （老逻辑）
3. 加 tests/test_hunt_store_read_switch.py：
   - 同一份数据，flag on vs off 读出字段完全一致
   - flag 切换期间并发读（SQLite 可见新写，JSON 也可见新写）
4. 灰度：先在 staging 跑 1 周
5. flip flag default to True 一次 commit
6. 监控：metrics 加 hunt_read_backend{backend=sqlite|json} counter
7. commit: "P1.3: flip hunt_store reads to SQLite (feature flag)"
```

**风险**：
- 读 JSON 期间 JSON 文件被删/损坏 → SQLite 已备份，flip flag 后不会丢数据
- 性能差异 → 加 benchmark（commit 内附 before/after 数字）

**回滚**：flip flag back to False 即可（commit 6 的逆操作）

### Wave 4 — JSON 退役 + 锁切换（10 月 22 日前完成）

**目标**：JSON 不再写入，`fcntl` 替换为 SQLite `hunt_locks`

```
1. hunt_store.py 移除 JSONBackend.write_* 调用（保留 .read() 1 周只读回退）
2. fcntl.flock 替换为 SQLite hunt_locks INSERT + UPDATE expires_at
3. 加 tests/test_hunt_locks.py：
   - 模拟 2 个 worker 并发 acquire 同一个 hunt_id，第二个等待或拒绝
   - worker 崩溃后 expires_at 过期，第二个 worker 可 acquire
4. 灰度 1 周：观察 metrics.hunt_lock_acquire_total / wait_ms
5. 删除 JSONBackend + _write_json_atomic
6. 把 backend/data/hunts/*.json 移到 archive/hunts-json-snapshot-2026-10-29/
7. commit: "P1.4: retire JSON backend + fcntl → SQLite hunt_locks"
```

**回滚**：从 archive 恢复 JSON + flip flag back to False（Wave 3 + Wave 4 的逆操作）

---

## 4. 测试策略

### 4.1 单元测试（每个 Wave 增量）

| 文件 | 测试数 | 覆盖 |
|---|---|---|
| `tests/test_migrations.py` | 8 (已有) | migrations 框架 |
| `tests/test_hunt_store_sqlite.py` | ~25 新增 | SQLiteBackend 12 方法 + 双写一致性 |
| `tests/test_migrate_hunts.py` | ~10 新增 | 回填脚本 dry-run + apply |
| `tests/test_hunt_store_read_switch.py` | ~12 新增 | flag on/off + 并发读 |
| `tests/test_hunt_locks.py` | ~8 新增 | fcntl 替换 + 崩溃恢复 |

目标：每个 Wave 后 pytest 1000 + 增量 ≥ 100% pass。

### 4.2 集成测试（Wave 3+4 必做）

- **真实数据迁移**：把 staging 的 20+ JSON 用 migrate 脚本导入 dev，flip flag 后跑 hunt API 30 分钟，metrics 无错误
- **并发 hunt 启动**：2 个 worker 同时跑同一 hunt_id，确认 hunt_locks 工作
- **崩溃恢复**：跑 hunt 中杀 -9 worker 模拟崩溃，确认下次 acquire 不死锁

### 4.3 性能 baseline（Wave 3 必做）

```
benchmark_hunt_store.py:
  load_hunt()        JSON:  X ms  |  SQLite: Y ms
  list_hunts()       JSON:  X ms  |  SQLite: Y ms
  dedup 100 leads    JSON:  X ms  |  SQLite: Y ms
  purge_old_hunts(30d) JSON: X ms  | SQLite: Y ms
```

结果写进 commit message（性能 regression 立即报警）。

---

## 5. 风险评估

| 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|
| 数据丢失（迁移中崩） | 低 | 高 | dry-run + audit.csv + INSERT OR IGNORE（重跑幂等）|
| 双写不一致 | 中 | 中 | Wave 1 只追加不替换；Wave 2 audit 脚本兜底 |
| 读切换后查询性能变差 | 低 | 中 | Wave 3 加 benchmark + index |
| SQLite WAL 锁与 fcntl 共存 | 中 | 低 | Wave 1-3 期间 fcntl 仍工作 |
| fcntl 锁被替换后崩溃死锁 | 低 | 高 | Wave 4 加 expires_at + sweep 任务 |
| 测试覆盖不足 | 中 | 高 | 每个 Wave 增量 ≥ 95% |

---

## 6. 时间估算

| Wave | 工作量 | 关键依赖 | 截止 |
|---|---|---|---|
| Wave 1 | 5-7 天 | 无 | 10 月 1 日 |
| Wave 2 | 2-3 天 | Wave 1 commit | 10 月 8 日 |
| Wave 3 | 3-4 天（含 1 周灰度）| Wave 2 commit + staging 数据 | 10 月 15 日 |
| Wave 4 | 3-5 天（含 1 周灰度）| Wave 3 commit | 10 月 22 日 |
| **合计** | **13-19 天** | | **10 月 22 日** |

---

## 7. 与现有工作的衔接

### 7.1 WIP 已完成的工作（无需重做）

- `hunt_store.py` 158 行 WIP 改动（双 backend ABC 抽象是**理想切入点**）
- `accept_new_leads` / `_dedup_hunt_paths` / `purge_old_hunts` WIP 实现
- `tests/test_api/test_hunt_dedup.py` WIP 集成测试
- `scripts/dedupe_hunt_leads.py` WIP 一次性去重脚本

### 7.2 P0 的产出（直接复用）

- `backend/migrations/runner.py` + `001_*.sql` —— P1 直接加 `002-006` 文件
- `apply_pending_migrations(conn)` —— hunt_store.py init_db 调用一次即可
- `tests/test_migrations.py` —— 加 002-006 测试

### 7.3 不在 P1 范围（明确 In/Out）

**In**：
- hunt JSON → SQLite 双 backend 迁移
- hunt_leads / hunt_stage_snapshots / hunt_cost_events 表
- fcntl → SQLite hunt_locks
- 4 Wave 灰度 + 回滚
- 测试 + benchmark

**Out**（保留给 P2/P3/P4）：
- Web 层无状态化（P2）
- SSE 事件化（P2）
- 巨石文件拆分（P3）
- Graph Webhook（P4）
- Scheduler 索引（P4）
- Prometheus metrics（P4）
- Docker 多阶段构建（P5）

---

## 8. 验收口径

### 8.1 Wave 1

- [ ] 002-006 migrations/*.sql 落地，`apply_pending_migrations` 应用成功
- [ ] `HuntBackendStorage` ABC + `SQLiteBackend` + `JSONBackend` 双写实现
- [ ] `tests/test_hunt_store_sqlite.py` ≥ 25 通过
- [ ] 全套 pytest 1025+ 通过
- [ ] 1 周观察 metrics 无 SQLite 写入错误

### 8.2 Wave 2

- [ ] `migrate_hunts_to_sqlite.py` dry-run 输出 audit.csv 为空
- [ ] apply 模式回填所有 20+ JSON
- [ ] `tests/test_migrate_hunts.py` ≥ 10 通过
- [ ] 全套 pytest 1035+ 通过

### 8.3 Wave 3

- [ ] feature flag 灰度 1 周
- [ ] `tests/test_hunt_store_read_switch.py` ≥ 12 通过
- [ ] benchmark_hunt_store.py 数字附在 commit message
- [ ] 全套 pytest 1047+ 通过
- [ ] flip flag default = True

### 8.4 Wave 4

- [ ] fcntl.flock 替换为 SQLite hunt_locks INSERT + expires_at
- [ ] `tests/test_hunt_locks.py` ≥ 8 通过
- [ ] 全套 pytest 1055+ 通过
- [ ] JSON 文件移到 archive
- [ ] hunt_lock_acquire_total metric 稳定无 deadlock 报警

---

## 9. 等用户决策

请告诉我：

| 选项 | 含义 |
|---|---|
| **A. 同意整个 P1 plan，开始 Wave 1** | 写 002-006 migrations + 双 backend ABC + 测试，预计 5-7 天 |
| **B. 只做 Wave 1，先看到结果** | 最小可交付：表 + 双写 + 测试，不做后续迁移路径 |
| **C. 调整优先级** | 比如先做 P4 Graph Webhook 或 P2 SSE，P1 推迟 |
| **D. 重写 plan** | 你有不同方案（比如直接 JSON → Postgres、不用 SQLite）|

**默认建议 A**（按 plan 原计划推进，最小惊讶）。