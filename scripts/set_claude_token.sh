#!/usr/bin/env bash
# 让 GitHub Actions 用你的 Claude 订阅做 AI 第二意见：
# 生成一年有效的 Claude token，存进仓库的 GitHub Secrets（CLAUDE_CODE_OAUTH_TOKEN）。
# token 过期后（一年）再运行一次即可。
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."

echo "第 1 步：在浏览器里授权，屏幕上会显示一个 token（只显示这一次）。"
env -u ANTHROPIC_BASE_URL -u ANTHROPIC_API_KEY claude setup-token
echo
read -rsp "第 2 步：把上面显示的 token 粘贴到这里后按回车（不会显示）: " token; echo
token="$(printf '%s' "$token" | tr -d '[:space:]')"
if [[ "$token" != sk-ant-oat* ]]; then
  echo "这不是 setup-token 生成的 token（应该以 sk-ant-oat 开头）。"
  echo "注意：浏览器授权页上显示的验证码是粘贴给第 1 步用的，不是这里要的 token。没有保存。"
  exit 1
fi
echo "正在用这个 token 试调用一次 Claude…"
reply="$(printf 'Reply with exactly: pong' | env -u ANTHROPIC_BASE_URL -u ANTHROPIC_API_KEY \
  CLAUDE_CODE_OAUTH_TOKEN="$token" claude -p --tools "" --no-session-persistence 2>&1 || true)"
if [[ "$reply" != *pong* ]]; then
  echo "这个 token 用不了，没有保存。Claude 返回：${reply:0:200}"; exit 1
fi
printf '%s' "$token" | gh secret set CLAUDE_CODE_OAUTH_TOKEN
# 清掉屏幕和滚动记录里的 token
printf '\033[3J\033c'
echo "已保存到 GitHub Secrets：CLAUDE_CODE_OAUTH_TOKEN。"
