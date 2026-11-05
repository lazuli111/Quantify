#!/usr/bin/env bash
# 把东京服务器每日更新要用的 Telegram 密钥传上去（用本机 .env 里已有的）。在这台电脑上运行，密钥不会显示在屏幕上。
# 先传到 claude-ops 账号下的临时文件，服务器上的安装命令（deploy/install.sh）会把它移到 Quantify 自己的目录并删掉临时文件。
# AI 第二意见在 GitHub 上跑，它的密钥在 GitHub Secrets 里（scripts/set_claude_token.sh、scripts/setup_deepseek.sh）。
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."
SERVER=tradingbot-tokyo

keys="$(grep -E '^(TELEGRAM_BOT_TOKEN|TELEGRAM_CHAT_ID)=' .env || true)"
for k in TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID; do
  grep -q "^$k=" <<<"$keys" || { echo "本机 .env 里没有 $k，先运行 scripts/setup_telegram.sh。"; exit 1; }
done
printf '%s\n' "$keys" | ssh "$SERVER" 'umask 077; cat > ~/quantify.env'
unset keys
echo "密钥已传到服务器（claude-ops 账号下的临时文件）。"
echo "下一步：用 admin 登录服务器，运行安装命令（见 deploy/install.sh 开头）。"
