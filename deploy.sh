#!/bin/bash
# ================================================================
# QQQ LEAPS 监控系统 — 服务器部署脚本
# 适用：Ubuntu 22.04 LTS（Oracle Cloud Free Tier / 任意 VPS）
# 用法：bash deploy.sh
# ================================================================
set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="python3"
VENV_DIR="$PROJECT_DIR/.venv"
CRON_TAG="qqq-leaps-monitor"

echo "================================================================"
echo " QQQ LEAPS 监控系统部署"
echo " 项目目录：$PROJECT_DIR"
echo "================================================================"

# ── 1. 系统依赖 ─────────────────────────────────────────────
echo ""
echo "[1/5] 检查系统依赖..."
if ! command -v python3 &>/dev/null; then
    echo "  安装 Python3..."
    sudo apt-get update -qq && sudo apt-get install -y python3 python3-pip python3-venv
fi
echo "  Python 版本：$(python3 --version)"

# ── 2. 虚拟环境 ─────────────────────────────────────────────
echo ""
echo "[2/5] 创建 Python 虚拟环境..."
if [ ! -d "$VENV_DIR" ]; then
    $PYTHON -m venv "$VENV_DIR"
    echo "  已创建：$VENV_DIR"
else
    echo "  已存在，跳过"
fi
source "$VENV_DIR/bin/activate"
pip install --quiet --upgrade pip
pip install --quiet -r "$PROJECT_DIR/requirements.txt"
echo "  依赖安装完成"

# ── 3. 环境变量 ─────────────────────────────────────────────
echo ""
echo "[3/5] 配置环境变量..."
ENV_FILE="$PROJECT_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
    cp "$PROJECT_DIR/.env.example" "$ENV_FILE"
    echo ""
    echo "  ⚠️  已创建 .env 文件，请填写以下内容后重新运行："
    echo "     vi $ENV_FILE"
    echo ""
    echo "  必填项："
    echo "    LARK_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/YOUR_ID"
    echo ""
    exit 0
else
    echo "  .env 已存在"
    # 检查 Webhook 是否已填写
    if grep -q "HOOK_ID_1" "$ENV_FILE"; then
        echo ""
        echo "  ⚠️  请先填写 LARK_WEBHOOK_URLS，然后重新运行此脚本"
        echo "     vi $ENV_FILE"
        exit 1
    fi
fi

# ── 4. Smoke Test ───────────────────────────────────────────
echo ""
echo "[4/5] 执行冒烟测试（dry-run）..."
cd "$PROJECT_DIR/src"
"$VENV_DIR/bin/python" main.py --dry-run
echo "  ✓ dry-run 通过"

# ── 5. 设置 Cron ────────────────────────────────────────────
echo ""
echo "[5/5] 配置定时任务（Cron）..."

# 移除旧的 cron 任务（若存在）
crontab -l 2>/dev/null | grep -v "$CRON_TAG" | crontab - 2>/dev/null || true

# 写入新任务
# 每周一至周五 UTC 10:00（= GMT+8 18:00）运行
# 若美股当日为假期，程序会自动检测并跳过
CRON_CMD="$VENV_DIR/bin/python $PROJECT_DIR/src/main.py >> $PROJECT_DIR/logs/cron.log 2>&1"
CRON_JOB="0 10 * * 1-5 $CRON_CMD # $CRON_TAG"

(crontab -l 2>/dev/null; echo "$CRON_JOB") | crontab -
echo "  已添加 Cron 任务："
echo "    $CRON_JOB"
echo ""
echo "  验证：crontab -l | grep $CRON_TAG"

# ── 完成 ─────────────────────────────────────────────────────
echo ""
echo "================================================================"
echo " 部署完成！"
echo ""
echo " 手动运行（测试）："
echo "   cd $PROJECT_DIR/src"
echo "   $VENV_DIR/bin/python main.py --dry-run    # 只打印，不推送"
echo "   $VENV_DIR/bin/python main.py              # 正式运行"
echo ""
echo " 查看日志："
echo "   tail -f $PROJECT_DIR/logs/cron.log"
echo "   ls $PROJECT_DIR/logs/"
echo ""
echo " 修改持仓后记得更新："
echo "   vi $PROJECT_DIR/config/positions.yaml"
echo "================================================================"
