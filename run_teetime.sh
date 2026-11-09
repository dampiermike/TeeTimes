#!/bin/bash
# Eldorado CC tee time booker — weekly runner (launchd: com.teetime.weekly, Sun 5:57 AM).
# Logs in early, waits for the 6:00:00 AM tee sheet release, books next Sunday.
# Logs to logs/teetime-YYYY-MM-DD_HHMMSS.log and emails the result.

set -eo pipefail

# Load credentials (ELDORADO_LOGIN/PASSWORD, GOOGLE_EMAIL/APP_PASSWORD, STOCKS_DIR)
source "$HOME/.bash_profile"

# ── Mode ──────────────────────────────────────────────────────────────────────
# 0 = dry run (opens each slot, verifies, releases — never clicks Finish Booking)
# 1 = live booking
LIVE=0
RELEASE_TIME="06:00:00"

PROJECT_SUBDIR="TeeTime"

if [ -z "$STOCKS_DIR" ]; then
    echo "ERROR: STOCKS_DIR is not set. Add 'export STOCKS_DIR=...' to ~/.bash_profile." >&2
    exit 1
fi
ROOT_DIR="$STOCKS_DIR/$PROJECT_SUBDIR"
cd "$ROOT_DIR"

PY="/usr/bin/python3"
LOG_DIR="logs"
mkdir -p "$LOG_DIR"
TS="$(date '+%Y-%m-%d_%H%M%S')"
LOG_FILE="$LOG_DIR/teetime-${TS}.log"

# ── Email alert helper ────────────────────────────────────────────────────────
# Usage: send_alert "subject" "/path/to/body_file"
send_alert() {
    local subject="$1"
    local body_file="$2"
    "$PY" -c '
import os, sys, smtplib
from email.mime.text import MIMEText
user = os.environ.get("GOOGLE_EMAIL", "")
pw   = os.environ.get("GOOGLE_APP_PASSWORD", "")
if not (user and pw):
    print("send_alert: GOOGLE_EMAIL/GOOGLE_APP_PASSWORD not set — skipping", file=sys.stderr)
    sys.exit(0)
subject = sys.argv[1]
with open(sys.argv[2], "r") as f:
    body = f.read()
msg = MIMEText(body)
msg["Subject"] = subject
msg["From"]    = user
msg["To"]      = user
try:
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pw)
        s.sendmail(user, [user], msg.as_string())
    print(f"alert sent: {subject}")
except Exception as e:
    print(f"alert email failed: {e}", file=sys.stderr)
' "$subject" "$body_file" 2>&1 | grep -v -i "urllib3\|warnings.warn" || true
}

ARGS=(--wait-until "$RELEASE_TIME")
[ "$LIVE" = "1" ] && ARGS+=(--live)
ARGS+=("$@")   # extra args for manual testing, e.g. --date 2026-11-15
MODE_LABEL=$([ "$LIVE" = "1" ] && echo "LIVE" || echo "DRY RUN")

# ── Run (all output tee'd to log file) ────────────────────────────────────────
set +e
{
    echo "=========================================="
    echo "  Tee Time Booker ($MODE_LABEL) — $(date '+%Y-%m-%d %H:%M:%S')"
    echo "=========================================="
    # caffeinate: keep the Mac awake for the whole run (it idle-slept 05:57–06:14 on 11/8)
    /usr/bin/caffeinate -i -s "$PY" book_teetime.py "${ARGS[@]}"
} 2>&1 | grep --line-buffered -v -i "urllib3\|warnings.warn" | tee "$LOG_FILE"
rc=${PIPESTATUS[0]}
set -e

RESULT_LINE="$(grep 'RESULT' "$LOG_FILE" | tail -n 1 | sed 's/^\[[^]]*\] //')"
[ -z "$RESULT_LINE" ] && RESULT_LINE="no result (crashed — see log)"

alert_body="/tmp/${PROJECT_SUBDIR}_$$.txt"
{
    echo "$RESULT_LINE"
    echo ""
    echo "Timestamp: $(date)"
    echo "Log file:  $ROOT_DIR/$LOG_FILE"
    echo ""
    echo "─── Log ───"
    tail -n 40 "$LOG_FILE"
} > "$alert_body"

if [ "$rc" -eq 0 ]; then
    send_alert "⛳ Tee times ($MODE_LABEL): ${RESULT_LINE#*) }" "$alert_body"
else
    send_alert "❌ Tee time booking FAILED ($MODE_LABEL, rc=$rc)" "$alert_body"
fi
rm -f "$alert_body"
exit "$rc"
