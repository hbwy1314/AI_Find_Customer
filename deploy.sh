#!/usr/bin/env bash
set -e

# AI Hunter 一键部署脚本
# 用法：./deploy.sh [--skip-frontend] [--skip-backend] [--no-restart]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REMOTE_HOST="root@169.58.171.85"
REMOTE_BASE="/opt/ai-hunter/repo"

SKIP_FRONTEND=false
SKIP_BACKEND=false
NO_RESTART=false

# 解析参数
for arg in "$@"; do
  case $arg in
    --skip-frontend)
      SKIP_FRONTEND=true
      shift
      ;;
    --skip-backend)
      SKIP_BACKEND=true
      shift
      ;;
    --no-restart)
      NO_RESTART=true
      shift
      ;;
    *)
      ;;
  esac
done

echo "==> AI Hunter 部署开始"
echo "    前端: $([ "$SKIP_FRONTEND" = true ] && echo "跳过" || echo "同步")"
echo "    后端: $([ "$SKIP_BACKEND" = true ] && echo "跳过" || echo "同步")"
echo "    重启: $([ "$NO_RESTART" = true ] && echo "否" || echo "是")"
echo ""

# 1. 构建前端
if [ "$SKIP_FRONTEND" = false ]; then
  echo "==> [1/5] 构建前端..."
  cd "$SCRIPT_DIR/frontend"
  bun run build
  echo "✓ 前端构建完成"
  echo ""
fi

# 2. 同步后端
if [ "$SKIP_BACKEND" = false ]; then
  echo "==> [2/5] 同步后端代码..."
  rsync -az \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.venv' \
    --exclude='data' \
    --exclude='uploads' \
    --exclude='*.db' \
    --exclude='*.db.bak*' \
    --exclude='*.seen' \
    --exclude='*.json' \
    "$SCRIPT_DIR/backend/agents" \
    "$SCRIPT_DIR/backend/api" \
    "$SCRIPT_DIR/backend/auth" \
    "$SCRIPT_DIR/backend/automation" \
    "$SCRIPT_DIR/backend/config" \
    "$SCRIPT_DIR/backend/emailing" \
    "$SCRIPT_DIR/backend/graph" \
    "$SCRIPT_DIR/backend/observability" \
    "$SCRIPT_DIR/backend/scripts" \
    "$SCRIPT_DIR/backend/tools" \
    "$SCRIPT_DIR/backend/main.py" \
    "$SCRIPT_DIR/backend/models.py" \
    "$SCRIPT_DIR/backend/requirements.txt" \
    "$SCRIPT_DIR/backend/pyproject.toml" \
    "${REMOTE_HOST}:${REMOTE_BASE}/backend/"
  echo "✓ 后端同步完成"
  echo ""
fi

# 3. 同步前端
if [ "$SKIP_FRONTEND" = false ]; then
  echo "==> [3/5] 同步前端构建产物..."
  rsync -az --delete \
    "$SCRIPT_DIR/frontend/dist/" \
    "${REMOTE_HOST}:${REMOTE_BASE}/frontend/dist/"
  echo "✓ 前端同步完成"
  echo ""
fi

# 4. 修复权限
echo "==> [4/6] 修复服务器文件权限..."
ssh "$REMOTE_HOST" bash <<'REMOTE_EOF'
set -e

# 修复 backend 目录归属和权限
chown root:aihunter /opt/ai-hunter/repo/backend
chmod 775 /opt/ai-hunter/repo/backend

# SQLite 需要对数据库文件和其父目录都有写权限，尤其是创建 journal/WAL 文件时。
find /opt/ai-hunter/repo/backend -maxdepth 1 \( -name "*.db" -o -name "*.db-*" \) -type f \
  -exec chown aihunter:aihunter {} \; \
  -exec chmod 664 {} \;

# 确保 data 目录归 aihunter 所有
if [ -d /opt/ai-hunter/repo/backend/data ]; then
  chown -R aihunter:aihunter /opt/ai-hunter/repo/backend/data
  chmod -R 755 /opt/ai-hunter/repo/backend/data
fi

# 确保 data/hunts 所有文件归 aihunter 可读写
if [ -d /opt/ai-hunter/repo/backend/data/hunts ]; then
  chown -R aihunter:aihunter /opt/ai-hunter/repo/backend/data/hunts
  find /opt/ai-hunter/repo/backend/data/hunts -type f -name "*.json" -exec chmod 644 {} \;
fi

# 确保 uploads 目录归 aihunter 所有
if [ -d /opt/ai-hunter/repo/backend/uploads ]; then
  chown -R aihunter:aihunter /opt/ai-hunter/repo/backend/uploads
  chmod -R 755 /opt/ai-hunter/repo/backend/uploads
fi

echo "✓ 权限修复完成"
REMOTE_EOF
echo ""

# 去重实时读取现存 Hunt 线索；部署不再清理合法公司名或修改历史线索。

# 6. 重启服务
if [ "$NO_RESTART" = false ]; then
  echo "==> [6/6] 重启服务..."
  ssh "$REMOTE_HOST" bash <<'REMOTE_EOF'
set -e
systemctl restart ai-hunter-api
sleep 5
echo "等待服务启动..."
for i in {1..12}; do
  sleep 2
  if systemctl is-active --quiet ai-hunter-api; then
    echo "✓ 服务启动成功"
    break
  fi
  if [ $i -eq 12 ]; then
    echo "✗ 服务启动超时"
    journalctl -u ai-hunter-api --since "1 minute ago" --no-pager | tail -20
    exit 1
  fi
done
REMOTE_EOF
  echo ""
fi

# 6. 健康检查
echo "==> [7/7] 健康检查..."
ssh "$REMOTE_HOST" bash <<'REMOTE_EOF'
set -e
HEALTH_CHECK=""
for i in {1..30}; do
  HEALTH_CHECK=$(curl -fsS https://api.nineluan.com/api/v1/health 2>/dev/null || true)
  if [[ "$HEALTH_CHECK" == *'"status":"ok"'* ]]; then
    echo "✓ 服务健康检查通过"
    echo "  响应: $HEALTH_CHECK"
    exit 0
  fi
  sleep 2
done

echo "✗ 服务健康检查失败"
echo "  最后响应: $HEALTH_CHECK"
journalctl -u ai-hunter-api --since "2 minutes ago" --no-pager | tail -20
exit 1
REMOTE_EOF
echo ""

echo "==> 部署完成！"
