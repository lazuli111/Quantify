#!/usr/bin/env bash
# 把东京服务器每日更新要用的密钥传上去。在这台电脑上运行，密钥不会显示在屏幕上：
# Telegram 和 DeepSeek 用本机 .env 里已有的；Claude（可以跳过）需要新生成一个一年有效的 token（GitHub 上那个不受影响）。
# 先传到 claude-ops 账号下的临时文件，服务器上的安装命令（deploy/install.sh）会把它移到 Quantify 自己的目录并删掉临时文件。
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."
SERVER=tradingbot-tokyo

keys="$(grep -E '^(TELEGRAM_BOT_TOKEN|TELEGRAM_CHAT_ID|DEEPSEEK_API_KEY)=' .env || true)"
for k in TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID DEEPSEEK_API_KEY; do
  grep -q "^$k=" <<<"$keys" || echo "提醒：本机 .env 里没有 $k，服务器上也不会有。"
done

token=""
read -rp "AI 意见要用你的 Claude 订阅吗？要就直接回车；不要（只用 DeepSeek，以后可以再运行这个脚本加上）输入 n 再回车：" use_claude
if [[ "$use_claude" != [nN]* ]]; then
  echo "第 1 步：在浏览器里授权，屏幕上会显示一个 token（只显示这一次）。"
  env -u ANTHROPIC_BASE_URL -u ANTHROPIC_API_KEY claude setup-token
  echo
  # token 很长，屏幕上会折成几行，复制时会带上换行：一直读到空行，再把各行拼起来。
  echo "第 2 步：把上面显示的 token 粘贴到这里（不会显示），然后再按一次回车（空行结束）:"
  while IFS= read -rs line; do
    [ -z "$line" ] && [ -n "$token" ] && break
    token+="$line"
  done
  echo
  token="$(printf '%s' "$token" | tr -d '[:space:]')"
  if [[ "$token" != sk-ant-oat* ]]; then
    echo "这不是 setup-token 生成的 token（应该以 sk-ant-oat 开头）。"
    echo "注意：浏览器授权页上显示的验证码是粘贴给第 1 步用的，不是这里要的 token。没有传到服务器。"
    exit 1
  fi
  echo "正在用这个 token 试调用一次 Claude…"
  reply="$(printf 'Reply with exactly: pong' | env -u ANTHROPIC_BASE_URL -u ANTHROPIC_API_KEY \
    CLAUDE_CODE_OAUTH_TOKEN="$token" claude -p --tools "" --no-session-persistence 2>&1 || true)"
  if [[ "$reply" != *pong* ]]; then
    echo "这个 token 用不了，没有传到服务器。Claude 返回：${reply:0:200}"; exit 1
  fi
fi

{
  printf '%s\n' "$keys"
  if [ -n "$token" ]; then printf 'CLAUDE_CODE_OAUTH_TOKEN=%s\n' "$token"; fi
} | ssh "$SERVER" 'umask 077; cat > ~/quantify.env'
unset token keys
# 清掉屏幕和滚动记录里的 token
printf '\033[3J\033c'
echo "密钥已传到服务器（claude-ops 账号下的临时文件）。"
echo "下一步：用 admin 登录服务器，运行对话里给你的安装命令。"
