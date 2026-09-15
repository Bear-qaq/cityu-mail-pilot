#!/usr/bin/env bash
# 把已导出的公开树推到 GitHub。
#
#   bash tools/publish_push.sh <账号>/<仓库名> [private|public]
#
# 为什么单独一个脚本：**这一步不可撤销**。推上去就收不回来（会被抓取、被 fork），
# 所以这个脚本把「它要做什么」全部打印出来、要求显式再确认一次，并在推之前
# 重新校验 `PUBLISH-MANIFEST.txt`——确保推上去的正是 `publish_export.py` 验过的那棵树，
# 而不是某个被手改过的目录。
#
# 认证：优先 SSH（`git@github.com:…`）。装了 `gh` 就用它建仓；**没装也能用**——
# 在网页上建好空仓库、把 SSH 公钥加到 GitHub 账号，这个脚本就只负责 push。
# （第一次就是这么做的：本机没有 gh 也没有 Homebrew，而 git/ssh 本来就在。）
#
# 先跑：`.venv-pilot/bin/python tools/publish_export.py --out dist/publish`
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"
TREE="$ROOT/dist/publish"
# 提交身份从环境变量取，**不写死**：这个脚本本身也要公开，而把运营者的邮箱
# 写进公开仓库的工具里，正是这个仓库的隐私扫描会拦下来的那种事（第一版就是，
# 被自己的闸门抓了）。要指定就：
#   PILOT_PUBLISH_NAME="你的名字" PILOT_PUBLISH_EMAIL="you@example.com" bash tools/publish_push.sh …
IDENTITY_NAME="${PILOT_PUBLISH_NAME:-CityU Mail Pilot}"
IDENTITY_EMAIL="${PILOT_PUBLISH_EMAIL:-cityu-mail-pilot@users.noreply.github.com}"

REPO="${1:-}"
VISIBILITY="${2:-private}"

if [ -z "$REPO" ]; then
  echo "用法：bash tools/publish_push.sh <账号>/<仓库名> [private|public]" >&2
  exit 2
fi
case "$VISIBILITY" in
  private|public) ;;
  *) echo "可见性只能是 private 或 public。" >&2; exit 2 ;;
esac

command -v git >/dev/null || { echo "没找到 git。" >&2; exit 2; }
[ -d "$TREE" ] || { echo "没有 $TREE。先跑：.venv-pilot/bin/python tools/publish_export.py --out dist/publish" >&2; exit 2; }

# 推之前重新校验：清单是导出时逐文件写的 sha256，任何手改都会在这里露出来。
echo "== 校验要推的这棵树 =="
( cd "$TREE" && shasum -a 256 -c PUBLISH-MANIFEST.txt >/dev/null ) \
  || { echo "清单校验失败：这棵树被改过。重新导出一份再推。" >&2; exit 3; }

REMOTE="git@github.com:$REPO.git"
CREATE_WITH_GH=""
echo "== 检查远端 =="
if git ls-remote "$REMOTE" >/dev/null 2>&1; then
  echo "  仓库可访问（空仓库就是我们要的）"
elif command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
  echo "  仓库还不存在，会用 gh 建一个（$VISIBILITY）"
  CREATE_WITH_GH="yes"
else
  echo "  连不上 $REMOTE，且没有可用的 gh。" >&2
  echo "  请在网页上建好空仓库、把 SSH 公钥加到 GitHub 账号，然后重跑。" >&2
  exit 2
fi

COUNT="$(find "$TREE" -type f | wc -l | tr -d ' ')"
echo
echo "即将执行（不可撤销）："
echo "  目录      $TREE"
echo "  文件数    $COUNT"
echo "  仓库      $REPO（$VISIBILITY）"
echo "  提交身份  $IDENTITY_NAME <$IDENTITY_EMAIL>"
echo "  分支      main"
echo
echo "这个仓库里不含：主密钥、任何 API key、邮箱授权码、真实用户邮箱、生产 IP。"
echo "它含  ：产品代码、测试、工具、选定的文档、AGPL-3.0 许可证。"
echo
if [ "${PILOT_PUBLISH_CONFIRM:-}" != "yes" ]; then
  read -r -p "确认推上去？输入 yes 继续：" answer
  [ "$answer" = "yes" ] || { echo "已取消。"; exit 1; }
fi

cd "$TREE"
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || git init -q -b main
git add -A
if ! git diff --cached --quiet; then
  git -c user.name="$IDENTITY_NAME" -c user.email="$IDENTITY_EMAIL" \
      commit -q -m "CityU Mail Pilot：首次公开

面向小规模内测的多用户邮件摘要服务。标准库为主，运行期只有一个第三方依赖
（cryptography）；只读 IMAP，不删信不转发；报告用大模型生成后从用户自己的
邮箱发出。详见 README.md 与 LICENSE（AGPL-3.0）。"
fi

if [ -n "$CREATE_WITH_GH" ]; then
  gh repo create "$REPO" "--$VISIBILITY" --source . --remote origin --push \
    --description "面向小规模内测的多用户邮件摘要服务（只读 IMAP + 大模型报告）"
else
  git remote get-url origin >/dev/null 2>&1 || git remote add origin "$REMOTE"
  git push -u origin main
fi

echo
echo "已推送：https://github.com/$REPO"
echo
echo "接下来两件事："
echo "  1) 在网页上把仓库看一遍（README 渲染、文件列表、GitHub 的 secret scanning 结果），"
echo "     确认没问题再切 public：Settings → General → 最下面 Danger Zone → Change visibility。"
echo "  2) 让官网指过去（生产上；值写进 0600 的环境文件，不经过命令行）："
echo "       sudo bash -c 'printf \"INFE_PILOT_SOURCE_URL=https://github.com/$REPO\\\\n\" >> /etc/cityu-mail-pilot/pilot.env'"
echo "       sudo systemctl restart cityu-mail-pilot-web"
echo "     重启后 / 与 /app 的页脚会出现「源代码（AGPL-3.0）」。"
