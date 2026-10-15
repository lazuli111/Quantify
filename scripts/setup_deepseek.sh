#!/usr/bin/env bash
# 把 DeepSeek API 密钥存到本机 .env 和 GitHub Secrets。输入时不会显示在屏幕上。
set -euo pipefail
cd "$(dirname "$0")/.."

printf '粘贴 DeepSeek API 密钥（sk- 开头），然后按回车：'
IFS= read -rs key
echo
key="${key//[[:space:]]/}"

if [[ ! "$key" =~ ^sk-[A-Za-z0-9]{20,}$ ]]; then
  echo "格式不对：密钥应该以 sk- 开头。请重新运行这个命令。"
  exit 1
fi
balance=$(curl -fsS -H "Authorization: Bearer ${key}" https://api.deepseek.com/user/balance || true)
if ! printf '%s' "$balance" | grep -q '"balance_infos"'; then
  echo "DeepSeek 不认这个密钥，请检查是否复制完整，然后重新运行。"
  exit 1
fi
if printf '%s' "$balance" | grep -q '"is_available":false'; then
  echo "提示：密钥有效，但账户余额不足，请先在 DeepSeek 平台充值。"
fi

touch .env && chmod 600 .env
if [ -s .env ] && [ -n "$(tail -c1 .env)" ]; then echo >> .env; fi
grep -v '^DEEPSEEK_API_KEY=' .env > .env.tmp || true
printf 'DEEPSEEK_API_KEY=%s\n' "$key" >> .env.tmp
mv .env.tmp .env && chmod 600 .env
echo "已保存到本机 .env"

printf '%s' "$key" | gh secret set DEEPSEEK_API_KEY --repo lazuli111/Quantify
echo "已保存到 GitHub Secrets"

unset key balance
echo "完成。回到对话告诉 Claude「好了」。"
