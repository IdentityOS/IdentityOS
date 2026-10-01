#!/usr/bin/env bash
#
# demo/aster_demo.sh — deterministic end-to-end demo of the Aster operator.
#
# Exercises, entirely offline with a file mailbox:
#   1. init (identity + capabilities + explicit grants)
#   2. supervised tick → individualized cold outreach (commitment policy)
#   3. idempotency: a second tick sends nothing new
#   4. inbound question → autonomous, in-thread reply
#   5. inbound opt-out → permanently honored
#   6. escalation: funding marked consequential → human decision required
#   7. decide --approve → message sent only after human authorization
#   8. pause / resume → no outreach while paused
#   9. status / needs / relationships / provenance inspection
#
# Usage:  bash demo/aster_demo.sh
# Output: a summary printed at the end; state lives in $TMPDIR (kept).

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

TMP="$(mktemp -d "${TMPDIR:-/tmp}/aster-e2e.XXXXXX")"
STORE="$TMP/store"
MAIL="$TMP/mail"
CANDS1="$TMP/candidates1.json"
CANDS2="$TMP/candidates2.json"

cat > "$CANDS1" <<'EOF'
{
  "candidates": [
    {
      "target_name": "Resonance Engineering Fund",
      "organization": "Resonance Engineering",
      "contact_email": "hello@resonance-engineering.com",
      "category": "funding",
      "relevant_work": ["open identity and compute infrastructure"],
      "fit_reason": "funds open infrastructure with persistent runtime work",
      "value_proposition": "open identity runtime with durable autonomous operator identities",
      "potential_ask": "information about your funding program and whether IdentityOS qualifies",
      "evidence": ["known public fund for open infrastructure"]
    }
  ]
}
EOF

cat > "$CANDS2" <<'EOF'
{
  "candidates": [
    {
      "target_name": "Remora Grants",
      "organization": "Remora Foundation",
      "contact_email": "grants@remora.example",
      "category": "funding",
      "relevant_work": ["open source infrastructure"],
      "fit_reason": "open-source infrastructure grants",
      "value_proposition": "open identity runtime with durable autonomous operator identities",
      "potential_ask": "information about the Remora grants program and whether IdentityOS qualifies",
      "evidence": ["public open-source grant program"]
    }
  ]
}
EOF

aster() { python3 -m cli.main aster "$@" ; }
export IDENTITY_MAILBOX_ROOT="$MAIL"

outbox_count() {
  python3 - "$MAIL/outbox-aster.json" <<'PY'
import json, sys
try:
    print(len(json.load(open(sys.argv[1]))))
except FileNotFoundError:
    print(0)
PY
}

last_thread() {
  python3 - "$MAIL/outbox-aster.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print(d[-1]["thread_id"])
PY
}

fail() { echo "FAIL: $1" >&2; exit 1; }

echo "== 1. init (store=$STORE mail=$MAIL) =="
aster init --store "$STORE" --project-root "$REPO" \
  --grant-email --grant-operations >/dev/null
# Aster defaults to conservative observation mode; the demo exercises the
# supervised autonomous path explicitly.
aster outbound-mode autonomous --store "$STORE" >/dev/null

echo "== 2. supervised tick (commitment policy → autonomous outreach) =="
aster tick --store "$STORE" --project-root "$REPO" \
  --candidates "$CANDS1" >/dev/null
[ "$(outbox_count)" -eq 1 ] || fail "expected 1 outreach, got $(outbox_count)"

echo "== 3. idempotency across processes =="
aster tick --store "$STORE" --project-root "$REPO" \
  --candidates "$CANDS1" >/dev/null
[ "$(outbox_count)" -eq 1 ] || fail "duplicate outreach: outbox=$(outbox_count)"

echo "== 4. inbound question → autonomous in-thread reply =="
THREAD="$(last_thread)"
cat > "$MAIL/inbox-aster.json" <<EOF
[{"external_id": "<q1@example>", "thread_id": "$THREAD",
  "from": "hello@resonance-engineering.com",
  "subject": "Re: Connecting IdentityOS with your work",
  "body": "Could you share details about the funding program's eligibility requirements for open infrastructure projects?"}]
EOF
aster tick --store "$STORE" --project-root "$REPO" \
  --candidates "$CANDS1" >/dev/null
[ "$(outbox_count)" -eq 2 ] || fail "expected autonomous reply, outbox=$(outbox_count)"

echo "== 5. inbound opt-out honored =="
cat > "$MAIL/inbox-aster.json" <<EOF
[{"external_id": "<optout@example>", "thread_id": "$THREAD",
  "from": "hello@resonance-engineering.com",
  "subject": "Re: Connecting IdentityOS with your work",
  "body": "Please stop contacting us about this."}]
EOF
aster tick --store "$STORE" --project-root "$REPO" \
  --candidates "$CANDS1" >/dev/null
[ "$(outbox_count)" -eq 2 ] || fail "opt-out still produced mail: outbox=$(outbox_count)"

echo "== 6. escalation (funding now consequential) =="
aster override --store "$STORE" require_approval_categories=funding >/dev/null
ESC="$(aster escalate --store "$STORE" 2>/dev/null | grep -o "message_id : [a-z0-9_]*" | awk '{print $3}' || true)"
if [ -z "$ESC" ]; then
    aster tick --store "$STORE" --project-root "$REPO" --candidates "$CANDS2" >/dev/null
    ESC="$(aster escalate --store "$STORE" 2>/dev/null | grep -o "message_id : [a-z0-9_]*" | awk '{print $3}' || true)"
fi
[ -n "$ESC" ] || fail "no escalation occurred"
[ "$(outbox_count)" -eq 2 ] || fail "send before authorization: outbox=$(outbox_count)"

echo "== 7. human decide --approve → sent only now =="
aster decide --store "$STORE" --message-id "$ESC" \
  --approve --note "demo review: proceed" >/dev/null
[ "$(outbox_count)" -eq 3 ] || fail "approved message not sent: outbox=$(outbox_count)"

echo "== 8. pause / resume =="
PENDING_BEFORE=$(aster status --store "$STORE" | grep -o '"pending_authorizations": [0-9]*' | awk '{print $2}')
aster pause --store "$STORE" >/dev/null
aster tick --store "$STORE" --project-root "$REPO" --candidates "$CANDS2" >/dev/null
[ "$(outbox_count)" -eq 3 ] || fail "sent while paused: outbox=$(outbox_count)"
aster resume --store "$STORE" >/dev/null
echo "paused tick sent nothing (pending held at $PENDING_BEFORE)"

echo "== results =="
NEEDS=$(aster needs --store "$STORE" | python3 -c "import json,sys;print(len(json.load(sys.stdin)['needs']))")
RELS=$(aster relationships --store "$STORE" | python3 -c "import json,sys;print(len(json.load(sys.stdin)['relationships']))")
PROV=$(aster provenance --store "$STORE" | python3 -c "import json,sys;print(len(json.load(sys.stdin)['provenance']))")
OBX=$(outbox_count)
echo "  store/state : $STORE (kept)"
echo "  mailbox     : $MAIL (kept)  outbox=$OBX records total"
echo "  needs       : $NEEDS  relationships: $RELS  provenance entries: $PROV"
echo "  outcome     : POS demo complete — every sent message is file-backed, "
echo "                escalation gated on a human decision, state survives restart."