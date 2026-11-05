#!/usr/bin/env bash
# 东京服务器上的每日更新，由 systemd 定时器在美股收盘后调用（deploy/systemd/）：
# 更新模拟账户 → 把记录推回 GitHub → Telegram 推送。
# GitHub 收到后发布网页（pages.yml），有新买入信号时再跑 AI 第二意见（ai.yml）。AI 不放在这台机器上：
# 它要同时开 4 个 Claude 进程，峰值超过 1 GB，这台 1.6 GB 的机器上还跑着交易机器人。
# 参数是这次运行的类型：
#   first   第一次（美东 23:10）：Yahoo 的当天数据还不全就安静结束，留给备用运行
#   backup  备用（美东 01:30）：第一次已成功时它发现没有新交易日，什么都不做；仍然不全才算失败并提醒
#   manual  手动（sudo systemctl start quantify-daily@manual）：总是推送
set -Eeuo pipefail
run="${1:-manual}"
cd "$(dirname "$(readlink -f "$0")")/.."

# 两次运行不要叠在一起
exec 9>"$HOME/daily.lock"
flock 9

alert() {
  # 第一次运行出错先不提醒，等备用运行
  [ "$run" = first ] && return
  .venv/bin/quantify notify --failed "东京服务器的 ${run} 运行出错。日志：journalctl -u quantify-daily@${run}" || true
}
trap alert ERR

# 上一次运行如果中途被打断，会留下没提交的改动，挡住 git pull。账户记录以已提交的为准，下面会重新算一遍。
git checkout -- paper docs
git clean -fdq paper docs
git pull --rebase --quiet
uv sync --frozen --quiet

# --soft-incomplete 跳过时会在 GITHUB_OUTPUT 指向的文件里写 skip=true（GitHub 上也是这样用的）
skip_file="$(mktemp)"
flags=()
[ "$run" = first ] && flags=(--soft-incomplete)
GITHUB_OUTPUT="$skip_file" .venv/bin/quantify paper "${flags[@]}"
if grep -q '^skip=true' "$skip_file"; then
  rm -f "$skip_file"
  exit 0
fi
rm -f "$skip_file"

git add paper docs
if git diff --cached --quiet; then
  echo "没有变化"
else
  git -c user.name="Quantify 东京服务器" -c user.email="quantify@tokyo.invalid" \
    commit --quiet -m "每日模拟更新 $(date -u +%F)"
  git push --quiet || { git pull --rebase --quiet && git push --quiet; }
fi

# 服务器自己的网页（nginx 的 /quantify/，见 deploy/nginx/）：只复制 docs/，代码和密钥不会对外
web_dir=/var/www/quantify
if [ -d "$web_dir" ]; then
  rsync -a --delete docs/ "$web_dir/"
fi

# 定时运行只在出现新交易日时推送；手动运行总是推送
if [ "$run" = manual ]; then
  .venv/bin/quantify notify --force || echo "Telegram 推送失败"
else
  .venv/bin/quantify notify || echo "Telegram 推送失败"
fi
