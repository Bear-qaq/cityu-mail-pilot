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
  guestbook_check
  demo_check
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

# A failing suite's diagnosis has to be readable somewhere. Step *logs* are not
# public: downloading them needs admin rights on the repository, so on a public
# repo a red run says "browser checks failed" and nothing else, to everyone who
# is not the owner. Check-run annotations *are* public. `::error::` becomes one,
# and since a literal newline would end the command they are escaped as %0A.
# This exists because the first CI run failed and the only way to find out why
# was to reproduce a Linux box from scratch.
annotate_failure() {
  local name="$1" log="$2" body
  [ -n "${GITHUB_ACTIONS:-}" ] || return 0
  # The body has to be ONE line. A workflow command ends at the first real
  # newline, so joining with %0A is not enough -- the newlines themselves have to
  # go. The first version of this only appended %0A at each line end and kept the
  # line breaks, which meant every annotation showed its first line and silently
  # dropped the rest; the one line that survived was never the failing one.
  body="$(tail -n 25 "$log" 2>/dev/null | tr -d '\r' \
          | sed -e 's/%/%25/g' \
          | awk '{ if (NR > 1) printf "%%0A"; printf "%s", $0 }')"
  [ -n "$body" ] || body="（没有输出）"
  echo "::error title=$name 失败::$body"
}

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

  # The admin suite is the only one that looks at the third light state: an
  # account with no key of its own whose reports ride the platform key. Without a
  # platform key in this environment that state cannot exist, so the chip would
  # only ever be asserted as a string in a Python test -- and the whole reason
  # this state exists is that it must not *look* like a fault.
  #
  # Only the search key is faked, and its base URL points at a closed local port
  # on purpose: the model key's base URL goes through the SSRF check (public
  # HTTPS only, `platform_model_default`), so it cannot be aimed somewhere
  # harmless, and no check in this repo is allowed to depend on a real vendor
  # answering. 搜索's base URL is used verbatim, so this one costs no network at
  # all -- the probe is refused by the kernel.
  platform_search_key=""
  platform_search_base=""
  [ "$name" = "admin_edit_check" ] && { platform_search_key="check-fixture-not-a-key"
                                       platform_search_base="http://127.0.0.1:9/"; }

  INFE_PILOT_DB="$db" \
  INFE_PILOT_MASTER_KEY="$MASTER" \
  INFE_PILOT_COOKIE_SECURE=0 \
  INFE_PILOT_ADMIN_EMAILS=boss@example.com \
  INFE_PILOT_SOURCE_URL="$source_url" \
  INFE_PILOT_DEFAULT_SEARCH_KEY="$platform_search_key" \
  INFE_PILOT_DEFAULT_SEARCH_BASE_URL="$platform_search_base" \
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
  suite_log="/tmp/check-${name}-suite.log"
  # Redirect to a *file*, not a pipe. Every suite ends with
  # `process.exit(failures.length ? 1 : 0)` right after printing its verdict, and
  # on Linux Node writes to a pipe asynchronously -- so `process.exit` discards
  # whatever has not been flushed yet, which is precisely the `FAILED (N): …`
  # line naming the assertions that broke. The first CI run showed this: the
  # annotation held 15 characters of the suite's last successful line and nothing
  # else. Writes to a file are synchronous on POSIX, so nothing is lost.
  {
    case "$name" in
      capacity_check)
        PILOT_ADMIN=boss@example.com node "tools/$name.js" "$base" "$shots" ;;
      security_ui_check)
        SHOTS_DIR="$shots" node "tools/$name.js" "$base" boss@example.com a-long-enough-password ;;
      *)
        PILOT_ADMIN=boss@example.com node "tools/$name.js" "$base" "$shots" ;;
    esac
  } > "$suite_log" 2>&1
  status=$?
  # Writing to a file costs the live output, which is the point of running these
  # by hand. Print it afterwards on a terminal; in CI stay quiet and let the
  # failure summary below be the thing that is read.
  [ -t 1 ] && cat "$suite_log"

  kill "$server" 2>/dev/null
  wait "$server" 2>/dev/null
  rm -f "$db" "$db"-*

  if [ "$status" -eq 0 ]; then
    PASSED+=("$name")
  else
    FAILED+=("$name")
    overall=1
    echo
    echo "──── $name 失败，输出尾部 ────"
    tail -n 25 "$suite_log"
    echo "───────────────────────────────"
    annotate_failure "$name" "$suite_log"
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
