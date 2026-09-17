# 空邮箱账号问题修复报告

## 问题描述
数据库中存在一个 `id='default'`、`from_email=''`、`from_name=''` 的空邮箱账号，删除后会自动重新出现。

## 根本原因
1. `api/email_routes.py:50` 的 `_default_account()` 函数会自动创建 `id="default"` 的邮箱账号
2. `config/settings.py:155-156` 中 `email_from_name` 和 `email_from_address` 的默认值都是空字符串 `""`
3. 当创建 Campaign 且未指定邮箱账号时，会调用 `_default_account()` 并使用空配置创建空账号

## 触发场景
- 创建 Campaign 时未选择具体邮箱账号 (Line 215, 382, 425, 518)
- 运行邮件回复检查任务 (Line 518)

## 修复方案

### 1. 删除数据库中的空账号
```bash
sqlite3 email_automation.db "DELETE FROM email_accounts WHERE id='default' AND from_email='';"
```

### 2. 修改 `_default_account()` 函数
**位置**: `backend/api/email_routes.py:50-91`

**修改内容**:
- 在创建账号前验证 `email_from_address` 不为空
- 如果配置为空，抛出 `ValueError` 并提示用户配置邮箱或选择已有账号
- 防止创建无效的空邮箱账号

**关键代码**:
```python
# Validate that email configuration is present
from_email = str(settings.email_from_address or "").strip()
from_name = str(settings.email_from_name or "").strip()
if not from_email:
    raise ValueError(
        "Default email account not configured. Please set email_from_address "
        "in settings or select a specific email account."
    )
```

### 3. 添加测试用例
**位置**: `backend/tests/test_api/test_email_routes.py:722-781`

**测试覆盖**:
- `test_default_account_rejects_empty_config`: 验证空配置被拒绝
- `test_default_account_creates_valid_account`: 验证有效配置正常工作

## 验证结果

### 本地验证
✓ 空配置被正确拦截，不会创建账号
✓ 有效配置正确创建了账号
✓ 数据库中无空邮箱账号

### 部署状态
✓ 代码已部署到线上
✓ 服务健康检查通过
✓ 权限修复完成

## 影响范围
- **用户体验**: 创建 Campaign 时如果未配置邮箱，会收到明确的错误提示，而不是创建无效账号
- **数据清洁**: 防止空邮箱账号反复出现
- **向后兼容**: 已配置邮箱的用户不受影响

## 后续建议
1. 在前端创建 Campaign 时，强制要求用户选择邮箱账号
2. 在系统设置页面增加邮箱配置验证提示
3. 定期检查数据库中是否有无效邮箱账号

## 相关文件
- `backend/api/email_routes.py`: 修复逻辑
- `backend/tests/test_api/test_email_routes.py`: 测试用例
- `backend/config/settings.py`: 配置定义
- `backend/emailing/store.py`: 邮箱账号存储

---
修复时间: 2026-09-16
修复版本: 当前部署
状态: 已完成并验证
