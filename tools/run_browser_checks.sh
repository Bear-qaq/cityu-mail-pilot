#!/usr/bin/env bash
# Run every browser check, each against its own freshly seeded preview server.
#
# Why one server per check: the suites register accounts and change the pilot
# cap, and the database outlives a run. Pointing them all at one server makes
# results depend on the order they happened to run in — an earlier suite fills
# the cap and a later one fails with "当前试点名额已满", which reads like a
# broken registration flow rather than leftover state. A fresh database per
# suite is the only way the result means anything.
#
#   bash tools/run_browser_checks.sh [only-this-check]
#
# Needs the Playwright install described in AGENTS.md §5.
set -uo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"
PY="$ROOT/.venv-pilot/bin/python"
MASTER="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
KEY="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
ONLY="${1:-}"

declare -a NAMES=(
  landing_check
  shell_check
  refresh_feedback_check
  install_hint_check
  capacity_check
  compliance_check
  tasks_check
  browser_check
  appearance_check
  background_photo_check
  metrics_check
  security_ui_check
  admin_edit_check
  usage_click_check
  admin_grant_check
  bulletin_check
  agent_action_check
  setup_guide_check
)

# A free port per suite, asked for rather than assumed. A fixed sequence
# starting at 9000 looked fine until two runs happened at once (a background run
# and a foreground one) -- they fought over the same ports, half the servers
# failed to bind, and every check in the loser reported a product failure. A
# tool whose result depends on what else is running is worse than no tool.
free_port() {
  "$PY" - <<'PYEOF'
import socket
sock = socket.socket()
sock.bind(("127.0.0.1", 0))
print(sock.getsockname()[1])
sock.close()
PYEOF
}

declare -a PASSED=() FAILED=() SKIPPED=()
overall=0

for name in "${NAMES[@]}"; do
  if [ -n "$ONLY" ] && [ "$ONLY" != "$name" ]; then continue; fi
  port="$(free_port)"
  db="/tmp/check-${name}.sqlite3"
  rm -f "$db" "$db"-*
  rm -rf "/tmp/shots-${name}"
  mkdir -p "/tmp/shots-${name}"

  echo
  echo "══════════════════════════════════════════════════════════════"
  echo "  $name   (port $port, fresh database)"
  echo "══════════════════════════════════════════════════════════════"

  # Production advertises its repository, so the landing-page suite must render
  # the same way -- otherwise the open-source block is never looked at by a real
  # browser and "it is on the page" is only ever asserted against a string.
  # Every other suite leaves it unset, which also keeps the "nothing is rendered
  # without a repository" branch in front of a browser.
  source_url=""
  [ "$name" = "landing_check" ] && source_url="https://github.com/JennieCN/cityu-mail-pilot"

  INFE_PILOT_DB="$db" \
  INFE_PILOT_MASTER_KEY="$MASTER" \
  INFE_PILOT_COOKIE_SECURE=0 \
  INFE_PILOT_ADMIN_EMAILS=boss@example.com \
  INFE_PILOT_SOURCE_URL="$source_url" \
  "$PY" -m pilot_app.web --host 127.0.0.1 --port "$port" > "/tmp/check-${name}.log" 2>&1 &
  server=$!

  for _ in $(seq 1 40); do
    curl -s -o /dev/null "http://127.0.0.1:$port/" && break
    sleep 0.25
  done

  base="http://127.0.0.1:$port"
  shots="/tmp/shots-${name}"
  seed_flags=""
  # admin_edit_check is the one suite that asserts on failure states (an
  # undelivered mail, spend rows); see _add_admin_fixtures for why those are not
  # in every preview. A plain string, not an array: bash 3.2 (macOS) treats
  # "${empty[@]}" as an unbound variable under `set -u`.
  case "$name" in
    admin_edit_check|agent_action_check) seed_flags="--admin-fixtures" ;;
  esac

  # Report a failed seed rather than swallowing it: it otherwise surfaces as a
  # suite failing on a missing account, which reads like a broken product.
  if ! INFE_PILOT_PREVIEW=1 INFE_PILOT_MASTER_KEY="$MASTER" \
       "$PY" tools/seed_preview.py "$db" --base "$base" $seed_flags \
       > "/tmp/seed-${name}.log" 2>&1; then
    echo "  播种失败，先看 /tmp/seed-${name}.log："
    sed -n '1,8p' "/tmp/seed-${name}.log"
  fi

  status=0
  case "$name" in
    capacity_check)
      PILOT_ADMIN=boss@example.com node "tools/$name.js" "$base" "$shots" || status=$? ;;
    security_ui_check)
      SHOTS_DIR="$shots" node "tools/$name.js" "$base" boss@example.com a-long-enough-password || status=$? ;;
    *)
      PILOT_ADMIN=boss@example.com node "tools/$name.js" "$base" "$shots" || status=$? ;;
  esac

  kill "$server" 2>/dev/null
  wait "$server" 2>/dev/null
  rm -f "$db" "$db"-*

  if [ "$status" -eq 0 ]; then
    PASSED+=("$name")
  else
    FAILED+=("$name")
    overall=1
  fi
done

echo
echo "══════════════════════════════════════════════════════════════"
echo "  passed: ${#PASSED[@]}  ${PASSED[*]:-}"
if [ "${#FAILED[@]}" -gt 0 ]; then
  echo "  FAILED: ${#FAILED[@]}  ${FAILED[*]}"
fi
echo "══════════════════════════════════════════════════════════════"
echo
echo "One suite adapts to its host rather than the code:"
echo "  metrics_check      — the CPU/RAM cards read /proc. macOS has no /proc, so"
echo "                       three assertions print 'skip' with the reason and are"
echo "                       counted; the Linux side is verified for real by"
echo "                       'manage check-metrics' on the server. A skip is not"
echo "                       a pass, which is why they are listed separately."
echo
echo "admin_edit_check now seeds its own failure fixtures (--admin-fixtures):"
echo "an undelivered mail, a skipped mail and token-usage rows. Three of its four"
echo "long-standing failures were that missing fixture; the fourth was real --"
echo "saving settings updated the audit summary but not the list behind it."
exit "$overall"
