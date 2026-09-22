# P0 阶段实施交付报告

> 版本 v1.0 | 日期 2026-09-22 | 类型 阶段交付 + 验证证据
> 分支 `p0/implementation` @ 71476c2
> 工作树 `../AI-Hunter-p0`（独立 git worktree，main WIP 在 stash@{0} 隔离）

---

## 0. 一页结论

| 计划项 | 状态 | 证据 |
|---|---|---|
| P0.1 依赖锁定（uv） | ✅ 完成 | uv.lock 261KB / 124 包 / Python 3.12.14 managed |
| P0.2 CI 后端启动冒烟 | ✅ 完成 | YAML 合法 / 本地 uvicorn 1s 启动 + /health 200 + 优雅关闭 |
| P0.3 Schema 版本机制 | ✅ 完成 | runner.py + 001_*.sql + 8/8 migrations 测试通过 |
| 测试基线 | ✅ 不退化 | 934 passed / 7 failed（同 baseline，7 失败是 main HEAD 回归与 P0 无关） |

**Commit**：
- `1af94b8` P0.1+P0.2 (5 files, +2592 / -96)
- `71476c2` P0.3 (5 files, +381)

---

## 1. 改动清单

### 新增文件
| 文件 | 行数 | 说明 |
|---|---|---|
| `backend/pyproject.toml` | 105 | 声明式依赖 + ruff/pytest 配置（合并 CI 配置） |
| `backend/uv.lock` | 2010 | uv-native 锁文件（含 transitive deps hash） |
| `backend/requirements.lock` | 369 | pip 兼容 shim（给没装 uv 的环境用） |
| `backend/migrations/runner.py` | 180 | SQLite migration 引擎（PRAGMA user_version + _migrations ledger） |
| `backend/migrations/001_create_migrations_ledger.sql` | 18 | 创 _migrations 表 + bump user_version 0→1 |
| `backend/tests/test_migrations.py` | 170 | 8 个迁移引擎测试 |

### 修改文件
| 文件 | 改动 |
|---|---|
| `backend/requirements.txt` | 30 个版本约束 → 单行 `-r requirements.lock`（向后兼容 shim） |
| `backend/emailing/store.py` | `init_db()` 入口前调 `apply_pending_migrations(conn)`（+2 行） |
| `backend/automation/job_queue.py` | 同上（+2 行） |
| `.github/workflows/ci.yml` | backend job 改用 uv；新增 backend-smoke job（+97 行） |

---

## 2. 关键设计决策

### 2.1 uv 而非 pip-compile
按 plan 推荐选 uv。优势：lock 含 transitive deps hash + 安装快 10-100x。
劣势：CI 加一个 action（`astral-sh/setup-uv@v6`）。已接受。

### 2.2 Python 3.12 managed
CI 是 3.12，本地是 3.9.6。设 `python-preference = "managed"` 让 uv 自动下载 3.12.14，保证本地/CI/锁一致。

### 2.3 requirements.lock 与 uv.lock 双 lock
- `uv.lock`：uv-native，含 hash，给 `uv sync --frozen` 用
- `requirements.lock`：pip 兼容（无 hash），给传统 `pip install -r` 用
- `requirements.txt`：占位 shim，引用 requirements.lock

这是计划 §P0.1 "uv 引入" 与 "CI 兼容旧环境" 的折衷。

### 2.4 Migration 引擎不抽离现有 DDL
P0.3 只做骨架（runner + 001 + audit ledger）。`emailing/store.py` 现有 16 个 `CREATE TABLE IF NOT EXISTS` **保留**，作为向后兼容兜底。
P1 会逐步把 DDL 抽到 `migrations/002~017.sql`，抽一个删一个 `IF NOT EXISTS`。

### 2.5 backend-smoke job 的 30s 超时
基于本地观察（启动 1s，CI runner 通常 < 5s），30s 留 6x 安全边际但避免 hang 浪费时间。

### 2.6 数据目录 sandbox
CI 用 `mkdir -p /tmp/aihunter-smoke` 隔离，不用真实 `backend/data/`，避免污染。

---

## 3. 验证证据

### 3.1 uv sync
```
$ uv sync --frozen --extra dev
Resolved 124 packages in 3.02s
+ aiohttp==3.14.3  + aiosqlite==0.22.1  + annotated-doc==0.0.5
+ ... (124 packages total)
+ yarl==1.25.1  + zipp==4.1.0  + zstandard==0.25.0
```

### 3.2 测试基线
```
$ uv run pytest -q
...
tests/test_api/test_email_routes.py::test_create_and_start_email_campaign FAILED
tests/test_api/test_email_routes.py::test_create_campaign_skips_blocked_template FAILED
tests/test_api/test_email_routes.py::test_create_campaign_skips_unapproved_sequences FAILED
tests/test_api/test_email_routes.py::test_create_campaign_includes_needs_review_when_approval_not_required FAILED
tests/test_api/test_email_routes.py::test_create_campaign_skips_previously_contacted_lead_email FAILED
tests/test_api/test_email_routes.py::test_run_email_reply_check_route FAILED
tests/test_api/test_email_routes.py::test_create_campaign_requires_smtp_configuration FAILED
========================= 7 failed, 934 passed, 24 warnings in 27.66s =========================
```

**新增通过 8 个**（vs baseline 926）：
- migrations 引擎 8 个测试

**7 失败与 P0 无关**：错误是 `assert "Microsoft Graph is not configured" in res.json()["detail"]`，实际是 `"No available email account found. All accounts have reached their send limits."`。这是 main HEAD `b3e6531 fix(email): auto-select account when none specified, rebind legacy 'default' campaigns` 引入的新逻辑（旧逻辑：错误信息变了但测试未更新）。

### 3.3 本地 uvicorn smoke
```
12:33:35 INFO  [observability.setup] [Observability] Langfuse disabled (LANGFUSE_ENABLED=false)
12:33:35 INFO  [api.app] [EmailScheduler] background loop started
12:33:35 INFO  [api.app] [EmailReply] background loop started
12:33:35 INFO  [api.app] [AutomationNotify] background loop started
12:33:35 INFO  [api.app] [TemplateSeedWorker] background loop started
12:33:35 INFO  [api.app] [AutomationConsumer] background loop started
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:18000
INFO:     127.0.0.1:58894 - "GET /api/v1/health HTTP/1.1" 200 OK
INFO:     127.0.0.1:58895 - "GET /api/v1/health HTTP/1.1" 200 OK
INFO:     Shutting down
...all 5 loops stopped cleanly...
INFO:     Application shutdown complete.
```
- 启动 1s
- 5 个后台 loop 全部 ready
- `/api/v1/health` 200
- 优雅关闭 1s

### 3.4 YAML 合法
```
$ uv run --no-project --with pyyaml python3 -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml').read()); print('YAML OK')"
Installed 1 package in 1ms
YAML OK
```

### 3.5 migrations 引擎测试
```
$ uv run pytest tests/test_migrations.py -v
tests/test_migrations.py::test_migration_filename_regex_accepts_valid_names PASSED
tests/test_migrations.py::test_migration_filename_regex_rejects_invalid_names PASSED
tests/test_migrations.py::test_list_migration_files_returns_sorted PASSED
tests/test_migrations.py::test_apply_pending_on_empty_db_creates_ledger_and_bumps_user_version PASSED
tests/test_migrations.py::test_apply_pending_is_idempotent PASSED
tests/test_migrations.py::test_legacy_user_version_is_marked_not_replayed PASSED
tests/test_migrations.py::test_email_store_init_db_runs_migrations PASSED
tests/test_migrations.py::test_job_queue_init_db_runs_migrations PASSED
========================= 8 passed in 0.05s =========================
```

### 3.6 ruff lint
```
$ uv run ruff check migrations/ tests/test_migrations.py
All checks passed!
```

---

## 4. 未验证项与已知限制

### 4.1 7 个 baseline 失败（与 P0 无关）
- 文件：`tests/test_api/test_email_routes.py`
- 根因：main HEAD commit `b3e6531` 引入账户自动选择逻辑，旧错误信息被替换，测试未适配
- 跟进：需另开 bug fix worktree 修测试（**不在 P0 范围**）

### 4.2 CI 真实运行未验证
- 原因：本地无 runner，需要 `git push` 触发
- 缓解：YAML 已本地 PyYAML 语法验证 + 关键命令本地跑过（`uv sync` / `uv run pytest`）

### 4.3 原 worktree WIP 状态
- main worktree `git stash` 后变干净，61 个 M 文件 WIP 在 `stash@{0}`
- p0 worktree 在 `../AI-Hunter-p0`，独立分支
- 后续：合并 p0 → main 后用户决定是否 `git stash pop` 恢复 WIP

### 4.4 Migration 引擎范围限制
- 当前只做骨架（runner + 001 ledger）
- 不替换现有 `CREATE TABLE IF NOT EXISTS` 块
- P1 必须做：把 `emailing/store.py` 16 个表抽到 `migrations/002~017.sql`

---

## 5. 回滚预案

如 P0 引入问题，每个 commit 可独立 revert：

```bash
# 整批回滚
cd /Users/yan/Desktop/project/AI\ Hunter/AI-Hunter-p0
git checkout main
git branch -D p0/implementation
git worktree remove ../AI-Hunter-p0 --force

# 单 commit 回滚
git revert 71476c2  # P0.3
git revert 1af94b8  # P0.1+P0.2
```

如只是 CI 失败，可只 revert `.github/workflows/ci.yml`：
```bash
git checkout main -- .github/workflows/ci.yml
```

---

## 6. 下一步建议

### 立即（用户决策）
1. **合并 p0 → main**？（推荐：用 fast-forward 或 squash）
2. **push 验证 CI**？（本地没 runner，必须 push 才能跑 backend-smoke job）
3. **继续 P1 数据层迁移**？（plan 第 1 周任务）

### 后续（与 P1 平行）
- 修 7 个 test_email_routes.py 失败（main HEAD b3e6531 回归）
- 决定 `git stash pop` 时机（合并 main 之后）

---

## 7. 关键文件位置

```
/Users/yan/Desktop/project/AI Hunter/
├── FInd_Customer/                                    ← 原 worktree (main)
│   ├── (WIP 在 stash@{0})
│   └── data/, .gitignore, etc.
│
└── AI-Hunter-p0/                                     ← p0 worktree (分支 p0/implementation)
    ├── backend/
    │   ├── pyproject.toml                            ← 新
    │   ├── uv.lock                                   ← 新
    │   ├── requirements.lock                         ← 新
    │   ├── requirements.txt                          ← 改为 shim
    │   ├── migrations/                               ← 新
    │   │   ├── runner.py
    │   │   └── 001_create_migrations_ledger.sql
    │   ├── emailing/store.py                         ← +2 行（init_db 调 runner）
    │   ├── automation/job_queue.py                   ← +2 行（同上）
    │   └── tests/test_migrations.py                  ← 新
    └── .github/workflows/ci.yml                      ← 大改（uv + smoke job）
```

---

> 报告结束。