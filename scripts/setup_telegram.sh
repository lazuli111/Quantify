#!/usr/bin/env bash
# 把 Telegram 机器人密钥存到本机 .env 和 GitHub Secrets。输入时不会显示在屏幕上。
set -euo pipefail
cd "$(dirname "$0")/.."

printf '粘贴 Telegram 机器人密钥（BotFather 给你的那串），然后按回车：'
IFS= read -rs token
echo
token="${token//[[:space:]]/}"

if [[ ! "$token" =~ ^[0-9]+:[A-Za-z0-9_-]{30,}$ ]]; then
  echo "格式不对：密钥应该类似 1234567890:AAH...。请重新运行这个命令。"
  exit 1
fi
if ! curl -fsS "https://api.telegram.org/bot${token}/getMe" | grep -q '"ok":true'; then
  echo "Telegram 不认这个密钥，请检查是否复制完整，然后重新运行。"
  exit 1
fi

# 本机：替换旧的一行（如果有），再写入新的
touch .env && chmod 600 .env
if [ -s .env ] && [ -n "$(tail -c1 .env)" ]; then echo >> .env; fi
grep -v '^TELEGRAM_BOT_TOKEN=' .env > .env.tmp || true
printf 'TELEGRAM_BOT_TOKEN=%s\n' "$token" >> .env.tmp
mv .env.tmp .env && chmod 600 .env
echo "已保存到本机 .env"

# GitHub：每天自动推送时使用
printf '%s' "$token" | gh secret set TELEGRAM_BOT_TOKEN --repo lazuli111/Quantify
echo "已保存到 GitHub Secrets"

unset token
echo "完成。回到对话告诉 Claude「好了」。"
