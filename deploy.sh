#!/bin/bash
# ==========================================
# 均线粘合选币器 - 一键部署脚本
# 用法: chmod +x deploy.sh && ./deploy.sh
# ==========================================
set -e

echo ">>> 春雷交易系统 v1 部署 <<<"

# 1. 装依赖
echo "[1/4] 安装 Python 依赖..."
pip3 install -r requirements.txt -q

# 2. 创建工作目录
echo "[2/4] 部署文件到 /opt/macd_bot ..."
mkdir -p /opt/macd_bot
cp *.py /opt/macd_bot/
cp *.json /opt/macd_bot/ 2>/dev/null || true
cp requirements.txt /opt/macd_bot/

# 3. 注册 systemd 服务
echo "[3/4] 注册开机自启服务..."
cp macd-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable macd-bot

# 4. 启动
echo "[4/4] 启动服务..."
systemctl restart macd-bot

echo ""
echo ">>> 部署完成 <<<"
echo "访问: http://$(hostname -I | awk '{print $1}'):5000"
echo ""
echo "常用命令:"
echo "  systemctl status macd-bot   # 查看状态"
echo "  systemctl restart macd-bot  # 重启"
echo "  systemctl stop macd-bot     # 停止"
echo "  tail -f /opt/macd_bot/bot.log  # 看日志"
