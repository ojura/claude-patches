#!/usr/bin/env bash
# End-to-end reproduction for the inline-mode resume scrollback-duplication bug.
#
# It generates a throwaway session whose rendered transcript overflows a short
# terminal, resumes it on the `claude` binary you point it at (inside tmux),
# captures the full scrollback, and checks whether any transcript line was
# stranded as a duplicate.
#
# Every rendered line carries a unique tag (MSGnn-HEAD for a message's first
# line, MSGnn-Lkk for its body). On a clean render each tag appears once. The
# bug re-emits whichever line lands on the scrollback/viewport seam, so that
# tag appears twice. A HEAD duplicate is the headline symptom (the message's
# first line shows up again above its body); a body duplicate is the same bug
# landing on a different row. A tall control run (transcript fits, no overflow)
# shows zero duplicates.
#
# Usage:  util/repro_resume_scrollback_dup.sh [path-to-claude]
# Requires: tmux, python3, and a logged-in `claude` (uses your real config;
#           creates and then deletes one throwaway project session).

set -euo pipefail

CLAUDE_BIN="${1:-$(command -v claude || true)}"
COLS=200
SHORT_ROWS=(46 48 50 52)   # overflow; sweep a few so the seam lands on a tag
TALL_ROWS=400              # transcript fits; control, expect zero duplicates
SETTLE=13                  # seconds to let the resumed view mount and settle

[ -x "$CLAUDE_BIN" ] || { echo "no executable claude at: '$CLAUDE_BIN'" >&2; exit 2; }
command -v tmux >/dev/null    || { echo "tmux required" >&2; exit 2; }
command -v python3 >/dev/null || { echo "python3 required" >&2; exit 2; }

echo "claude: $CLAUDE_BIN ($("$CLAUDE_BIN" --version 2>/dev/null))"

WORK="$(mktemp -d /tmp/ccreproXXXXXX)"            # no dots: claude maps . to - in project dirs
SID="$(python3 -c 'import uuid;print(uuid.uuid4())')"
ENC="$(printf '%s' "$WORK" | sed 's#/#-#g; s#\.#-#g')"
PROJ_DIR="$HOME/.claude/projects/$ENC"
TMUX_SESS="ccrepro_$$"

cleanup() {
  tmux kill-session -t "$TMUX_SESS" 2>/dev/null || true
  rm -rf "$WORK" "$PROJ_DIR"
}
trap cleanup EXIT
mkdir -p "$PROJ_DIR"

python3 - "$SID" "$WORK" > "$PROJ_DIR/$SID.jsonl" <<'PY'
import json, sys, uuid
from datetime import datetime, timezone, timedelta
sid, cwd = sys.argv[1], sys.argv[2]
t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
def ts(i): return (t0 + timedelta(seconds=i)).isoformat().replace("+00:00", "Z")
def base(i):
    return dict(isSidechain=False, userType="external", entrypoint="cli", cwd=cwd,
                sessionId=sid, version="2.1.168", gitBranch="main", timestamp=ts(i))
recs, parent = [], None
for k in range(40):
    uu = str(uuid.uuid4())
    recs.append(dict(base(2*k), parentUuid=parent, type="user", uuid=uu,
                     promptId=str(uuid.uuid4()), promptSource="typed",
                     permissionMode="bypassPermissions", slug="resume-scrollback-repro",
                     message=dict(role="user", content=f"UMSG{k:02d}")))
    au = str(uuid.uuid4())
    body = (f"MSG{k:02d}-HEAD unique header line for message {k:02d}\n\n" +
            "\n".join(f"MSG{k:02d}-L{j:02d} body line {j} of message {k:02d}" for j in range(3)))
    recs.append(dict(base(2*k+1), parentUuid=uu, type="assistant", uuid=au, requestId=f"req_{k}",
                     message=dict(model="claude-opus-4-6", id=f"msg_{k}", type="message",
                                  role="assistant", content=[dict(type="text", text=body)],
                                  stop_reason="end_turn", stop_sequence=None,
                                  usage=dict(input_tokens=1, output_tokens=1))))
    parent = au
for r in recs:
    print(json.dumps(r))
PY

echo "session: $SID ($(wc -l < "$PROJ_DIR/$SID.jsonl") records), throwaway project $(basename "$PROJ_DIR")"

# resume at COLS x rows, return the duplicated tags (one per line, empty if none)
dups_at() {
  local rows="$1"
  tmux kill-session -t "$TMUX_SESS" 2>/dev/null || true
  tmux new-session -d -s "$TMUX_SESS" -x "$COLS" -y "$rows"
  tmux send-keys -t "$TMUX_SESS" "cd $WORK && $CLAUDE_BIN -r $SID" Enter
  sleep "$SETTLE"
  local cap; cap="$(tmux capture-pane -p -S - -t "$TMUX_SESS")"
  tmux send-keys -t "$TMUX_SESS" Escape 2>/dev/null || true
  tmux send-keys -t "$TMUX_SESS" C-c 2>/dev/null || true; sleep 0.4
  tmux send-keys -t "$TMUX_SESS" C-c 2>/dev/null || true; sleep 0.5
  tmux kill-session -t "$TMUX_SESS" 2>/dev/null || true
  printf '%s\n' "$cap" | grep -oE 'MSG[0-9]+-(HEAD|L[0-9]+)' | sort | uniq -d
}

reproduced=0
echo
echo "overflowing runs (${COLS} cols):"
for rows in "${SHORT_ROWS[@]}"; do
  d="$(dups_at "$rows")"
  if [ -n "$d" ]; then
    reproduced=1
    head_hit=""; printf '%s\n' "$d" | grep -q HEAD && head_hit="  <- message header duplicated"
    echo "  ${COLS}x${rows}: STRANDED $(printf '%s' "$d" | tr '\n' ' ')${head_hit}"
  else
    echo "  ${COLS}x${rows}: clean (seam landed on a blank row)"
  fi
done

echo "control run (transcript fits):"
dc="$(dups_at "$TALL_ROWS")"
echo "  ${COLS}x${TALL_ROWS}: ${dc:+STRANDED }${dc:-clean}"

echo
if [ "$reproduced" -eq 1 ] && [ -z "$dc" ]; then
  echo "REPRODUCED: a transcript line is duplicated into scrollback when the view overflows, and not when it fits."
  exit 0
else
  echo "NOT reproduced this run (overflow dup=$reproduced, control dup='${dc:-none}')."
  echo "On a slow box raise SETTLE; otherwise widen SHORT_ROWS so the seam lands on a tagged row."
  exit 1
fi
