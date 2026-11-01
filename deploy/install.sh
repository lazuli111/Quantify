#!/usr/bin/env bash
# 在东京服务器上安装 Quantify 每天的自动更新。用 admin 账号登录服务器后运行：
#   sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/lazuli111/Quantify/main/deploy/install.sh)"
# 重复运行是安全的（已经做过的步骤会跳过），以后更新设置也是再运行一次。
# 只会新建 quantify 账号和 /opt/quantify 目录，不会改动 /opt/tradingbot 和交易机器人的服务。
set -euo pipefail
cd /

USER_NAME=quantify
HOME_DIR=/opt/quantify
APP=$HOME_DIR/app
BIN=$HOME_DIR/.local/bin
REPO_HTTPS=https://github.com/lazuli111/Quantify.git
REPO_SSH=git@github.com:lazuli111/Quantify.git
UV_VERSION=0.13.6
CLAUDE_VERSION=2.1.301   # 和 GitHub Actions 里装的版本一致
CLAUDE_SHA512=JNe0VzIuXdaBrtlh+fY2SX5AQLDf8BDfIKrylOWK528AZ/aAq2bJt4SBkPikZ0CvecUbL0YN22Xe7eltAWCJGg==
GITHUB_HOST_KEY="github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
OPS=claude-ops           # Claude 维护服务器用的账号：能看日志、手动触发一次更新，读不到密钥

[ "$(id -u)" -eq 0 ] || { echo "请用 sudo 运行"; exit 1; }
as_q() { sudo -u "$USER_NAME" -H "$@"; }

echo "1/7 新建运行账号 $USER_NAME（不能登录，只用来跑这个任务）"
id "$USER_NAME" >/dev/null 2>&1 ||
  useradd --system --create-home --home-dir "$HOME_DIR" --shell /usr/sbin/nologin "$USER_NAME"
chmod 750 "$HOME_DIR"

echo "2/7 下载代码到 $APP"
[ -d "$APP/.git" ] || as_q git clone --quiet "$REPO_HTTPS" "$APP"

echo "3/7 安装 uv $UV_VERSION 和项目依赖（第一次要几分钟）"
if [ "$(as_q "$BIN/uv" --version 2>/dev/null | cut -d' ' -f2)" != "$UV_VERSION" ]; then
  as_q sh -c "curl -LsSf https://astral.sh/uv/$UV_VERSION/install.sh | sh -s -- --no-modify-path --quiet"
fi
as_q sh -c "cd $APP && $BIN/uv sync --frozen --extra ai --quiet"

echo "4/7 安装 Claude Code $CLAUDE_VERSION（AI 第二意见用）"
if [ "$(as_q "$BIN/claude" --version 2>/dev/null | cut -d' ' -f1)" != "$CLAUDE_VERSION" ]; then
  tmp="$(mktemp -d)"
  curl -fsSL -o "$tmp/claude.tgz" \
    "https://registry.npmjs.org/@anthropic-ai/claude-code-linux-x64/-/claude-code-linux-x64-$CLAUDE_VERSION.tgz"
  if [ "$(openssl dgst -sha512 -binary "$tmp/claude.tgz" | base64 -w0)" != "$CLAUDE_SHA512" ]; then
    rm -rf "$tmp"; echo "下载的 Claude Code 校验不通过，停止安装"; exit 1
  fi
  tar -xzf "$tmp/claude.tgz" -C "$tmp" package/claude
  install -o "$USER_NAME" -g "$USER_NAME" -m 755 "$tmp/package/claude" "$BIN/claude"
  rm -rf "$tmp"
fi

echo "5/7 推送记录用的 GitHub 部署密钥"
key=$HOME_DIR/.ssh/id_github
as_q mkdir -p -m 700 "$HOME_DIR/.ssh"
[ -f "$key" ] || as_q ssh-keygen -q -t ed25519 -N "" -C "quantify@tokyo" -f "$key"
echo "$GITHUB_HOST_KEY" | as_q tee "$HOME_DIR/.ssh/known_hosts" >/dev/null
as_q git -C "$APP" config core.sshCommand "ssh -i $key -o IdentitiesOnly=yes"
as_q git -C "$APP" remote set-url origin "$REPO_SSH"
# 公钥（不是机密）留一份给 Claude，由它加到 GitHub 仓库
id "$OPS" >/dev/null 2>&1 && install -o "$OPS" -g "$OPS" -m 644 "$key.pub" "/home/$OPS/quantify-deploy-key.pub"

echo "6/7 密钥（Telegram / DeepSeek / Claude）"
if [ -f "/home/$OPS/quantify.env" ]; then
  install -o "$USER_NAME" -g "$USER_NAME" -m 600 "/home/$OPS/quantify.env" "$APP/.env"
  rm -f "/home/$OPS/quantify.env"
  echo "  已从 $OPS 的临时文件移到 $APP/.env（只有 $USER_NAME 能读），临时文件已删除"
fi
if [ ! -f "$APP/.env" ]; then
  echo "  还没有密钥：先在你的电脑上运行 scripts/setup_server_secrets.sh，再运行一次这个安装命令"
elif grep -q '^CLAUDE_CODE_OAUTH_TOKEN=' "$APP/.env"; then
  reply="$(as_q bash -c 'cd '"$APP"' && export CLAUDE_CODE_OAUTH_TOKEN="$(sed -n "s/^CLAUDE_CODE_OAUTH_TOKEN=//p" .env)" &&
    printf "Reply with exactly: pong" | DISABLE_AUTOUPDATER=1 '"$BIN"'/claude -p --tools "" --no-session-persistence 2>&1' || true)"
  if [[ "$reply" == *pong* ]]; then
    echo "  Claude 订阅在服务器上可以用"
  else
    echo "  Claude 在服务器上调用失败（AI 意见会改用 DeepSeek）：${reply:0:200}"
  fi
fi

echo "7/7 定时运行（美东 23:10，备用 01:30）"
install -m 644 "$APP"/deploy/systemd/quantify-daily@.service "$APP"/deploy/systemd/quantify-daily-*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now --quiet quantify-daily-first.timer quantify-daily-backup.timer
if id "$OPS" >/dev/null 2>&1; then
  rule=/etc/sudoers.d/quantify-ops
  printf '%s ALL=(root) NOPASSWD: /usr/bin/systemctl start quantify-daily@manual.service, /usr/bin/systemctl start --no-block quantify-daily@manual.service\n' "$OPS" > "$rule.tmp"
  chmod 440 "$rule.tmp"
  if visudo -cqf "$rule.tmp"; then mv "$rule.tmp" "$rule"; else rm -f "$rule.tmp"; echo "  sudo 规则检查不通过，没有写入"; fi
fi

echo
systemctl list-timers 'quantify-*' --no-pager
echo
echo "安装完成。回到对话告诉 Claude「装好了」，它会把部署密钥加到 GitHub，再手动跑一次检查。"
