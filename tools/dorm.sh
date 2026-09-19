#!/usr/bin/env bash
#
# 一条指令，把活交给宿舍那台 Windows 机（WSL2 里的 ~/ban）。
#
# 装成一个命令（**只装一次**，之后在 Mac 的任何目录都能用）：
#
#   bash tools/dorm.sh install            # 在 ~/bin 里放一个 dorm，并把 ~/bin 加进 PATH
#   dorm                                  # 不给参数 = 现在什么状态 + 怎么用
#   dorm status                           # 隧道通不通 / 那台在哪个提交、干不干净
#   dorm '<命令>'                          # 直接在宿舍机上执行（例：dorm 'df -h'）
#   dorm ask '<一句话>'                    # 让那台的 DSH 干一件事（headless，跑在 tmux 里，断线也不中断）
#   dorm job [id] / dorm jobs             # 派过的活干到哪儿了
#   dorm watch [会话]                      # 在 Mac 终端里实时看那台干活（默认看 dorm-jobs）
#   dorm handoff [一句话]                  # 【最常用】Mac 收工 → 宿舍机接上 → 那台的 DSH 接着干
#   dorm handoff --fast                   # 同上，但跳过 write（只在简报与当前树一致时才行）
#   dorm handoff --only                   # 只交接、不派活（你想自己在宿舍机上接着干）
#
# 没装那个命令时，上面的 `dorm` 全部写成 `bash tools/dorm.sh`（两者完全等价）。
#
# 为什么要有它：换机器本来要人记六条命令（write → commit → push → pull → verify → 开工），
# 记错一条的代价是「另一台报指纹漂移，而你看不出是谁动的」。这里把那六步钉死成一条指令，
# **每一步失败都当场停下来说清楚为什么**，绝不带着一个不确定的树往下走。
#
# 它不碰秘密、不推公开树、也不替你决定「要不要部署」—— 那些仍然按 AGENTS.md 的铁律走。
#
# 退出码：3 = 连不上宿舍机（隧道没开）；4 = 前提不成立（pull 或 verify 没过）；127 = 命令找不到。
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="${DORM_HOST:-dorm}"
PY="$ROOT/.venv-pilot/bin/python"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=8)

say() { printf '%s\n' "$*"; }

die() {   # $1 = 退出码，$2… = 话
  local code="$1"; shift
  printf '\n[停] %s\n' "$*" >&2
  exit "$code"
}

dorm_ok() { ssh "${SSH_OPTS[@]}" "$HOST" 'true' >/dev/null 2>&1; }

# 连不上时**不能只说「连不上」**：那扇门是宿舍机自己开的（crontab @reboot + ~/.bashrc 里的
# ~/bin/dorm-tunnel.sh，掉了 30 秒内自己重连），所以要么那台没开机，要么看护循环不在跑。
# 这一段把两种情形分开说，并把要敲的那一行原样给出来。
need_dorm() {
  dorm_ok && return 0
  cat >&2 <<'MSG'

[停] 连不上宿舍机 —— 那扇门（反向隧道）现在没开。

     门是**宿舍机自己开**的：WSL 一起来就跑 ~/bin/dorm-tunnel.sh，掉了 30 秒内自己重连。
     所以先看那台 Windows 是不是关着 / WSL 没起来（开一下，等 30 秒再试）。

     如果它开着还是不通，在**那台的 Ubuntu 窗口**里敲这两行：

         pgrep -af dorm-tunnel.sh || (setsid nohup ~/bin/dorm-tunnel.sh >/dev/null 2>&1 &)
         ssh -o ConnectTimeout=8 ubuntu@203.0.113.10 true

     再回 Mac 这边跑 `dorm status`（没装那个命令就 `bash tools/dorm.sh status`）。
MSG
  exit 3
}

# ---------------------------------------------------------------- 状态

cmd_status() {
  local mac_head mac_ver mac_dirty
  mac_head="$(git -C "$ROOT" log --oneline -1)"
  mac_ver="$(grep -m1 '__version__' "$ROOT/pilot_app/__init__.py" | cut -d'"' -f2)"
  mac_dirty="$(git -C "$ROOT" status --porcelain | wc -l | tr -d ' ')"
  say "本机（Mac）  $mac_head"
  say "             v$mac_ver · 未提交 $mac_dirty 处"
  if ! dorm_ok; then
    say "宿舍机       **连不上**（隧道没开）"
    need_dorm   # 这里会带着那两行命令退出 3
  fi
  say "宿舍机       隧道 通"
  ssh "${SSH_OPTS[@]}" "$HOST" bash -s <<'EOS'
cd ~/ban 2>/dev/null || { echo "             （那台还没有 ~/ban）"; exit 0; }
printf '             %s\n' "$(git log --oneline -1)"
printf '             v%s · 未提交 %s 处\n' \
  "$(grep -m1 '__version__' pilot_app/__init__.py | cut -d'"' -f2)" \
  "$(git status --porcelain | wc -l | tr -d ' ')"
printf '             隧道进程 %s 个 · 派过的活 %s 个\n' \
  "$(pgrep -fc 'R 2222:localhost:22' 2>/dev/null || echo 0)" \
  "$(ls ~/dorm-jobs/*.task 2>/dev/null | wc -l | tr -d ' ')"
EOS
}

# ---------------------------------------------------------------- 在那边跑东西

cmd_exec() {
  [ "$#" -ge 1 ] || die 2 "用法：bash tools/dorm.sh exec '<shell 命令>'"
  need_dorm
  # 原样带回输出与退出码：这一条是「我在这边敲、在那台执行」，不是「帮我做点什么」。
  ssh "${SSH_OPTS[@]}" "$HOST" "cd ~/ban && $*"
}

# 把一次 headless DSH 跑成**后台任务**（tmux 窗口 + 日志文件），而不是挂在 ssh 会话上：
# 干活可能要十几分钟，Mac 这边合盖、断网、Ctrl-C 都不该让那台的工作半路死掉。
start_job() {
  local task="$1"
  local id="job-$(date +%m%d-%H%M%S)"
  need_dorm
  # 任务文本先落到那台的文件里再跑：省得引号/换行在 ssh → tmux → dsh 三层里被拆坏。
  printf '%s\n' "$task" | ssh "${SSH_OPTS[@]}" "$HOST" \
    "mkdir -p ~/dorm-jobs && cat > ~/dorm-jobs/$id.task"
  # 跑法写在那边的一个小脚本里（内容固定、可复查），tmux 只负责「别断电就死」。
  # 这个脚本放在 ~/dorm-jobs/ 下 —— **仓库外**，免得给另一台的工作区添乱。
  ssh "${SSH_OPTS[@]}" "$HOST" 'cat > ~/dorm-jobs/run.sh && chmod +x ~/dorm-jobs/run.sh' <<'RUN'
#!/bin/bash
# 在宿舍机的仓库里跑一次 headless DSH。用法：run.sh <任务 id>
set -u
id="$1"
cd "$HOME/ban" || exit 9
log="$HOME/dorm-jobs/$id.log"
{ echo "[任务] $(cat "$HOME/dorm-jobs/$id.task")"
  echo "[开始] $(date -u +%Y-%m-%dT%H:%M:%SZ)"; } > "$log"
dsh --profile headless "$(cat "$HOME/dorm-jobs/$id.task")" >> "$log" 2>&1
echo "[exit=$?] [结束] $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$log"
RUN
  ssh "${SSH_OPTS[@]}" "$HOST" \
    "tmux has-session -t dorm-jobs 2>/dev/null || tmux new-session -d -s dorm-jobs -n idle; \
     tmux new-window -t dorm-jobs -n '$id' 'bash \$HOME/dorm-jobs/run.sh $id'"
  say ""
  say "已派活：$id（在那台的 tmux 会话 dorm-jobs 里跑；Mac 这边想走随时可以走）"
  say "       那台屏幕上想看：tmux attach -t dorm-jobs（Ctrl+B 松手再按 D 退出观看）"
  follow_job "$id"
}

# 轮询式跟随：每 2 秒问一次「有没有新行 / 跑完没有」。
# 不用 `tail -f` 是因为它会把 ssh 会话挂住 —— 而这条通道本来就时断时续。
follow_job() {
  local id="$1" seen=0 total
  say ""
  say "── 它在干什么（Ctrl-C 只是不看了，活照跑） ────────────────"
  while :; do
    total="$(ssh "${SSH_OPTS[@]}" "$HOST" "wc -l < ~/dorm-jobs/$id.log 2>/dev/null || echo 0" | tr -d ' ')"
    total="${total:-0}"
    if [ "$total" -gt "$seen" ]; then
      ssh "${SSH_OPTS[@]}" "$HOST" "sed -n '$((seen + 1)),${total}p' ~/dorm-jobs/$id.log"
      seen="$total"
    fi
    if ssh "${SSH_OPTS[@]}" "$HOST" "grep -q '^\[exit=' ~/dorm-jobs/$id.log 2>/dev/null"; then
      break
    fi
    sleep 2
  done
  # 收尾再拉一次：`[exit=` 那一行是循环判定的同时写进去的，不补这一下，
  # 屏幕上就永远看不到退出码 —— 而「跑完了没有、跑成没跑成」正是要看的那个东西。
  total="$(ssh "${SSH_OPTS[@]}" "$HOST" "wc -l < ~/dorm-jobs/$id.log 2>/dev/null || echo 0" | tr -d ' ')"
  if [ "${total:-0}" -gt "$seen" ]; then
    ssh "${SSH_OPTS[@]}" "$HOST" "sed -n '$((seen + 1)),${total}p' ~/dorm-jobs/$id.log"
  fi
  local verdict code
  verdict="$(ssh "${SSH_OPTS[@]}" "$HOST" "grep -m1 '^\[exit=' ~/dorm-jobs/$id.log" || true)"
  code="${verdict#\[exit=}"; code="${code%%\]*}"
  say "──────────────────────────────────────────────────────────"
  say "结果：exit=${code:-?}（0 = 正常跑完）· 完整日志：dorm job $id"
}

cmd_ask() {
  [ "$#" -ge 1 ] || die 2 "用法：bash tools/dorm.sh ask '<让那台的 DSH 干什么>'"
  start_job "$*"
}

cmd_job() {
  need_dorm
  local id="${1:-}"
  if [ -z "$id" ]; then
    id="$(ssh "${SSH_OPTS[@]}" "$HOST" \
      'ls -t ~/dorm-jobs/*.log 2>/dev/null | head -1 | xargs -r basename | sed "s/\.log$//"')"
    [ -n "$id" ] || die 4 "宿舍机上还没派过活。"
  fi
  say "── $id ──────────────────────────────────────────────────"
  ssh "${SSH_OPTS[@]}" "$HOST" "cat ~/dorm-jobs/$id.log 2>/dev/null || echo '(没有这个任务的日志)'"
}

cmd_jobs() {
  need_dorm
  ssh "${SSH_OPTS[@]}" "$HOST" bash -s <<'EOS'
cd ~/dorm-jobs 2>/dev/null || { echo "（还没派过活）"; exit 0; }
for t in *.task; do
  [ -e "$t" ] || continue
  id="${t%.task}"
  if tmux list-windows -t dorm-jobs -F '#{window_name}' 2>/dev/null | grep -qx -- "$id"; then
    st='跑着'
  elif grep -q '^\[exit=' "$id.log" 2>/dev/null; then
    st="跑完（$(grep -m1 '^\[exit=' "$id.log")）"
  else
    st='没跑起来 / 会话被关了（看 ' "$id" '.log）'
  fi
  printf '  %s  %-18s %s\n' "$(date -r "$id.log" '+%m-%d %H:%M' 2>/dev/null || echo '??')" "$id" "$st"
done
EOS
}

# ---------------------------------------------------------------- 交接

# 默认那句「接着干」：把项目现成的四句话（见 docs/second-machine-2026-09-18.md §11）
# 与「从欠账清单里挑一条做完」接在一起。它是一段**提示词**，不是命令 —— 所以写进文件，
# 让那台的 DSH 自己读。
handoff_prompt() {
  local fp="$1"
  cat <<EOF
你在一台 Windows 机的 WSL2 里接手这个项目，仓库是 ~/ban。Mac 那台刚刚收工，
交接指纹是 $fp（你刚跑过 verify，与本机逐位相同）。

请按顺序做：
1. 读 AGENTS.md §2/§6、handoff/HANDOFF.md、docs/open-items-2026-09-14.md。
2. 用**一句话**报告：现在是什么状态 / 还剩什么 / 你不该碰什么。
3. 从欠账清单里挑**最上面那条自己能做完的**继续做；按 AGENTS.md §8 收工：
   跑测试 → handoff.py write → 改了 pilot_app/ 就部署并线上验收 → commit → push。
   做完把结论写进 HANDOVER.md 与对应的 docs/。

不许：推倒重来、把秘密写进仓库/日志、动真实用户的账号。卡住就问，别猜。
EOF
}

cmd_handoff() {
  local fast=0 only=0 task=""
  while [ "$#" -ge 1 ]; do
    case "$1" in
      --fast) fast=1; shift ;;
      --only) only=1; shift ;;
      *) task="$*"; break ;;
    esac
  done

  # ⓪ 先摸一下那扇门。**放在最前面**：write 要跑三分钟，若隧道没开，那三分钟纯属白等 ——
  #    而「连不上」这件事现在就查得出来。
  need_dorm

  # ① Mac 这边收工。
  #    树**不干净不是错误**：交接本来就发生在「刚干完活」的时候，那些改动正是要交出去的东西。
  #    但必须让人看见它们（下一步它们会被提交并推给另一台），所以先列出来。
  local dirty
  dirty="$(git -C "$ROOT" status --porcelain)"
  if [ -n "$dirty" ]; then
    say "① 这次一起交出去的、还没提交的改动："
    printf '%s\n' "$dirty" | sed 's/^/     /'
  fi
  #    只拦一种情况：明显不该进仓库的产物（跑预览剩下的库、日志、打包产物）。
  #    这类东西一旦被 `git add -A` 带上去，另一台就会拿到一个脏工作区，而且很难看出来是谁的。
  local junk
  junk="$(printf '%s\n' "$dirty" | awk '{print $NF}' \
    | grep -E '(^|/)(dist|node_modules|__pycache__)/|\.(log|sqlite3?|db|tar\.gz|pyc)$' || true)"
  if [ -n "$junk" ]; then
    printf '\n[停] 这些看着不该进仓库，先删掉或加进 .gitignore：\n' >&2
    printf '%s\n' "$junk" | sed 's/^/     /' >&2
    exit 4
  fi
  if [ "$fast" = 0 ]; then
    say "① Mac 收工：跑全量单测并写交接简报（约 3 分钟，别打断）…"
    ( cd "$ROOT" && "$PY" tools/handoff.py write --agent mac ) \
      || die 4 "write 没过（上面有红字）—— 修完再来，别把没验过的树交给另一台。"
  else
    # `--fast` 省掉的是「跑三分钟测试 + 重新生成简报」，但**简报只对「没改过的树」有效**：
    # 只要这一轮有新改动（或者上次是改完没 write 就提交的），简报描述的就不是当前这棵树，
    # 那台的 verify 会当场报「指纹漂移」。所以这里先花一秒核一下 ——
    # 2026-09-19 第一次拿 `--fast` 补记文档时就撞上了：跳过 write、提交了文档、那台立刻报漂移。
    local recorded
    recorded="$("$PY" "$ROOT/tools/handoff.py" status 2>/dev/null | grep -m1 '记录指纹')"
    case "$recorded" in
      *一致*)
        say "① 跳过 write（--fast）：简报与当前这棵树一致，直接用它"
        ;;
      *)
        die 4 "--fast 不能用在「树改过」的时候：$recorded —— 跑不带 --fast 的 handoff，让它重新生成简报。"
        ;;
    esac
  fi

  # ② 提交并推。远端可能已经有别人的提交（两台机器都会推），所以先 fetch，
  #    被拒时**不 force**：那条路只会把别人的工作抹掉。
  say "② Mac 提交并推送…"
  ( cd "$ROOT" \
    && git add -A \
    && { git diff --cached --quiet || git commit -q -m "交接：$(date -u '+%Y-%m-%dT%H:%MZ') 从 Mac 交给宿舍机"; } \
    && git fetch -q origin \
    && git push -q origin HEAD:main ) \
    || die 4 "提交/推送失败 —— 多半是远端有别人的新提交：`git -C \"$ROOT\" pull --rebase` 之后再来。"

  # ③ 宿舍机接上：pull → verify。verify 是**这条链上唯一不能省的判据** ——
  #    它重算指纹并重跑断言，通过了才说明两边是同一棵树。
  need_dorm
  say "③ 宿舍机接上：pull + verify（指纹必须与本机逐位相同，那台要跑几分钟）…"
  ssh "${SSH_OPTS[@]}" "$HOST" 'cd ~/ban && git pull --ff-only' \
    || die 4 "宿舍机 pull 失败（那边有未提交改动或已经分叉）—— 去那台敲 `cd ~/ban && git status`。"
  ssh "${SSH_OPTS[@]}" "$HOST" 'cd ~/ban && .venv-pilot/bin/python tools/handoff.py verify' \
    || die 4 "宿舍机 verify 没过 —— 交接到此为止，别接着往下做（先看那台的红字）。"

  local fp
  # `handoff.py status` 那一行是「指纹<空格>哈希（N 个文件）」—— 中文全角括号紧贴着哈希，
  # 按空格切会连「（299」一起吃进来（第一版就是这么错的）。所以只取开头那一段十六进制。
  fp="$("$PY" "$ROOT/tools/handoff.py" status 2>/dev/null \
    | grep -m1 '^指纹' | sed 's/^指纹 *//; s/[^0-9a-f].*$//')"
  say "   两边指纹一致：$fp"

  if [ "$only" = 1 ]; then
    say ""
    say "④ --only：不派活。那台已经就绪，你在它的 DSH 界面里接着干就行。"
    say "   **从现在起让宿舍机当写者**，Mac 这边不要再改这棵树（否则下一轮指纹会漂）。"
    return 0
  fi

  say "④ 派活给那台的 DSH…"
  if [ -n "$task" ]; then
    start_job "$task"
  else
    start_job "$(handoff_prompt "$fp")"
  fi
  say ""
  say "交接完成。**从现在起写者是宿舍机**：Mac 这边下次要用，先"
  say "    cd \"$ROOT\" && git pull --ff-only && dorm status"
}

usage() {
  # 打印文件开头那段注释（到第一个非注释行为止）。**别再写死行号** ——
  # 第一版是 `sed -n '3,17p'`，头部一加内容就把它截断了。
  awk 'NR > 2 && /^#/ { sub(/^# ?/, ""); print; next } NR > 2 { exit }' "$0"
  say ""
  say "现在："
  cmd_status
}

# ---------------------------------------------------------------- 装成一条命令

# 在 Mac 上装一个 `dorm`：之后在**任何目录**都能 `dorm '<命令>'`、`dorm handoff`。
# 生成的只是一个转发壳（里面钉着这个仓库的绝对路径）—— 逻辑仍然只有这一份，
# 免得出现「~/bin 里那份是旧的」这种最难查的漂移。
cmd_install() {
  local bin="$HOME/bin" target="$HOME/bin/dorm"
  mkdir -p "$bin"
  cat > "$target" <<EOF
#!/bin/sh
# 在 Mac 上命令宿舍机。**别改这个文件** —— 它由 $ROOT/tools/dorm.sh install 生成。
# 要改行为，改那个脚本（这个壳只负责把参数原样传过去）。
SELF="$ROOT/tools/dorm.sh"
[ -f "\$SELF" ] || {
  echo "dorm: 找不到 \$SELF —— 仓库搬过家了？到新位置重新跑一次 bash tools/dorm.sh install" >&2
  exit 127
}
exec /bin/bash "\$SELF" "\$@"
EOF
  chmod +x "$target"
  say "已装好：$target"

  case ":$PATH:" in
    *":$bin:"*)
      say "PATH 里已经有 $bin —— 直接用就行"
      ;;
    *)
      if grep -qs 'export PATH="\$HOME/bin:\$PATH"' "$HOME/.zshrc" 2>/dev/null; then
        say "~/.zshrc 里已经有那一行 PATH（只是当前这个终端还没生效）"
      else
        if [ -f "$HOME/.zshrc" ]; then
          cp "$HOME/.zshrc" "$HOME/.zshrc.bak-$(date +%Y%m%d%H%M%S)"
          say "（原 ~/.zshrc 已备份一份）"
        fi
        printf '\n# CityU Mail Pilot：在 Mac 上命令宿舍机（bash tools/dorm.sh install 加的）\nexport PATH="$HOME/bin:$PATH"\n' >> "$HOME/.zshrc"
        say "已把 $bin 加进 ~/.zshrc 的 PATH"
      fi
      ;;
  esac

  say ""
  say "让它生效：新开一个终端窗口，或者现在跑一次  source ~/.zshrc"
  say ""
  say "之后在 Mac 的任何目录都能这么用（和 bash tools/dorm.sh 完全等价）："
  say "    dorm                   现在什么状态 + 用法"
  say "    dorm '<命令>'           直接在宿舍机上执行   （例：dorm 'df -h'）"
  say "    dorm ask '<一句话>'     让那台的 DSH 干一件事"
  say "    dorm handoff           关 Mac 之前：交接 + 让那台接着干"
  say "    dorm jobs / job        派过的活 / 看最新那个的日志"
  say "    dorm watch             在 Mac 终端里实时看那台干活"
}

# ---------------------------------------------------------------- 在 Mac 上看那台干活

# `ssh -t` 是必须的：tmux 要有终端才画得出界面。
cmd_watch() {
  need_dorm
  local session="${1:-dorm-jobs}"
  say "看那台的 tmux 会话「$session」——"
  say "  Ctrl+B 松手再按 D = 退出观看（**不影响它继续跑**）；那台的 Ubuntu 窗口里也能这么看"
  say ""
  ssh -t "${SSH_OPTS[@]}" "$HOST" \
    "tmux attach -t '$session' || { echo; echo '（没有这个会话。那台上现在有：）'; tmux ls; echo; echo '干活时会有 dorm-jobs 这个会话；想看我平时干活的 shell 就敲：dorm watch mac'; }"
}

case "${1:-}" in
  ''|help|-h|--help) usage ;;
  status)            cmd_status ;;
  exec)              shift; cmd_exec "$@" ;;
  ask)               shift; cmd_ask "$@" ;;
  job)               shift; cmd_job "$@" ;;
  jobs)              cmd_jobs ;;
  watch)             shift; cmd_watch "$@" ;;
  install)           cmd_install ;;
  handoff)           shift; cmd_handoff "$@" ;;
  *)
    # 不是子命令 → **当成「在宿舍机上执行这一条」**（这就是「直接在 Mac 上命令宿舍机」：
    # `dorm df -h`、`dorm 'git log --oneline -1'`）。先打一行说明，
    # 免得有人以为这句话是跑在本机的。
    say "→ 在宿舍机上执行：$*"
    cmd_exec "$@"
    ;;
esac
