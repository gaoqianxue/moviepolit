#!/usr/bin/env bash
# 把 MoviePolit 插件推送到你的 Git 仓库（Linux / macOS / 容器内）
#
# 用法：
#   ./push.sh https://github.com/<用户名>/<仓库名>.git [分支]
#   GIT_TOKEN=ghp_xxx ./push.sh https://github.com/<用户名>/<仓库名>.git main
#   GIT_PROXY=http://127.0.0.1:7897 ./push.sh https://github.com/<用户名>/<仓库名>.git
#
# token 只作为推送时的参数临时使用，不会写入 .git/config。

set -uo pipefail

REPO_URL="${1:-}"
BRANCH="${2:-main}"
GIT_TOKEN="${GIT_TOKEN:-}"
GIT_PROXY="${GIT_PROXY:-${HTTPS_PROXY:-${https_proxy:-}}}"

cd "$(dirname "$0")"

if [ -z "$REPO_URL" ]; then
  echo "用法: ./push.sh <仓库地址> [分支]" >&2
  exit 1
fi

# 所有要走网络的 git 命令都带上代理（仅本次调用生效，不写入 .git/config）
GITNET=()
if [ -n "$GIT_PROXY" ]; then
  GITNET=(-c "http.proxy=$GIT_PROXY" -c "https.proxy=$GIT_PROXY")
  echo "使用代理: $GIT_PROXY"
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

# origin 永远只写「不带 token」的地址；带 token 的地址只在推送时作为参数临时使用，
# 这样任何失败路径都不可能把 token 留在 .git/config 里。
git remote remove origin >/dev/null 2>&1 || true
git remote add origin "$REPO_URL"

# 先探测远端是否可达，把「连不上」和「没权限/仓库不存在」区分开
if ! git "${GITNET[@]}" ls-remote "$PUSH_URL" >/dev/null; then
  echo "无法访问远端仓库：请确认仓库已创建、地址正确、token 有效，且网络/代理可用" >&2
fi

if ! git "${GITNET[@]}" push "$PUSH_URL" "refs/heads/${BRANCH}:refs/heads/${BRANCH}"; then
  echo "推送失败，尝试与远端合并后重试..."
  git "${GITNET[@]}" pull --rebase origin "$BRANCH"
  git "${GITNET[@]}" push "$PUSH_URL" "refs/heads/${BRANCH}:refs/heads/${BRANCH}" || {
    echo "推送失败：$REPO_URL ($BRANCH) 未推送成功，请检查上面的 git 输出" >&2
    exit 1
  }
fi

echo "推送完成：$REPO_URL ($BRANCH)"

# 顺带推送版本 tag（失败不影响主流程）
if [ -n "$(git tag)" ]; then
  git "${GITNET[@]}" push "$PUSH_URL" --tags && echo "已推送 tag: $(git tag | tr '\n' ' ')"
fi

echo "提示：建议在 GitHub 上把仓库设为公开，MoviePilot 插件市场才能直接拉取。"
