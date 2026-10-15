#!/usr/bin/env bash
# 把 MoviePolit 插件推送到你的 Git 仓库（Linux / macOS / 容器内）
#
# 用法：
#   ./push.sh https://github.com/<用户名>/<仓库名>.git [分支]
#   GIT_TOKEN=ghp_xxx ./push.sh https://github.com/<用户名>/<仓库名>.git main
#
# 推送结束后会把 remote 地址还原成不带 token 的版本。

set -uo pipefail

REPO_URL="${1:-}"
BRANCH="${2:-main}"
GIT_TOKEN="${GIT_TOKEN:-}"

cd "$(dirname "$0")"

if [ -z "$REPO_URL" ]; then
  echo "用法: ./push.sh <仓库地址> [分支]" >&2
  exit 1
fi

if [ ! -d .git ]; then
  git init -b "$BRANCH"
fi
[ -n "$(git config user.name || true)" ] || git config user.name "MoviePolit"
[ -n "$(git config user.email || true)" ] || git config user.email "moviepolit@local"

git add -A
if git diff --cached --quiet; then
  echo "没有需要提交的变更"
else
  git commit -m "feat: MoviePolit 流媒体上线监控插件 v1.0.0"
fi

PUSH_URL="$REPO_URL"
if [ -n "$GIT_TOKEN" ] && [[ "$REPO_URL" == https://* ]]; then
  BARE="${REPO_URL#https://}"
  case "$REPO_URL" in
    *github.com*) PUSH_URL="https://x-access-token:${GIT_TOKEN}@${BARE}" ;;
    *)            PUSH_URL="https://oauth2:${GIT_TOKEN}@${BARE}" ;;
  esac
fi

git remote remove origin >/dev/null 2>&1 || true
git remote add origin "$PUSH_URL"

if ! git push -u origin "$BRANCH"; then
  echo "推送失败，尝试与远端合并后重试..."
  git pull --rebase origin "$BRANCH"
  git push -u origin "$BRANCH"
fi

git remote set-url origin "$REPO_URL"
echo "完成：$REPO_URL ($BRANCH)"
