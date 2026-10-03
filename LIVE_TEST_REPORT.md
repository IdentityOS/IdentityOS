# Live Gmail Test — Findings, Fixes, and Re-Run Plan

Two live-test runs informed this work: the **first** (July) ran against the real
Gmail box and surfaced six failures in the then-current

BROWSER-BRIDGE build. The **second run** described in this plan is the
permissioned `.aster-live-v2` re-run on a **clean store**, exercising the fixes
below.

Branch: `feat/live-browser-bridge`
Corrective commits: `af4ef28` (inbound/policy/confidence/etc.), the live-run-
found cursor fixes (STATUS + literal extraction, finding 17), and the reply-
integrity fixes found when the human answered the outreach thread (findings
18–20).

Full suite after fixes: **1127 passed / 43 skipped** with no LLM credentials in
the environment (43 = 40 network/browser skips + 3 cross-app skips). With real
credentials the suite additionally runs `test_cross_app_continuity.py`
(previously dead: `NameError: repo_root`, now fixed); its remaining failure is
a **pre-existing** adapter-chain problem verified against a clean baseline
worktree — see "Pre-existing issues (not from this work)" below.

---

## The 16 findings → root cause → fix

### 1. Unrelated/system mail was treated as part of a conversation
The engine reacted to every inbound message within any existing thread, even
genuine third-party or system mail that merely shared headers.
**Fix:** `ConversationMonitor` in `core/operations/monitor.py` now classifies
every message through `_classify()` into an `InboundDisposition`
(`TRUSTED_THREAD` / `APPROVED_SENDER` / `UNSOLICITED_UNKNOWN` / `AUTOMATED` /
`BOUNCE` / `SELF_COPY` / `SPAM_OR_BULK`). Only trusted categories may reach a
reply.

### 2. `no-reply@` automation was answered as if it were the counterparty
No relationship existed with the automation address, but abuses of thread
context slipped through.
**Fix:** an automated-sender regex (`no-reply`, `donotreply`, `mailer-daemon`,
`postmaster`, …) plus bounce/automated/spam subject-and-body patterns are the
secondary gate. Automated mail is `ignored`: recorded for audit, never replied
to, and **never** used to forge or mutate a relationship.

### 3. Mail arriving in a thread but from an unexpected sender was trusted
Header matching alone was treated as identity.
**Fix:** a trusted-thread disposition still requires `_norm(sender) ==
_norm(relationship.email)`. A mismatch is a **thread intrusion** → `quarantined`
and the rule tests prove the relationship email is never reassigned to the
intruder.

### 4. Self-copies and mailbox duplicates could double-process mail
SMTP sends landed back in the same IMAP folder the monitor reads.
**Fix:** `SELF_COPY` is matched against `self_address` (wired from
`config.sender_email`) and skipped; cursor dedupe (finding 15) prevents
re-processing across restarts.

### 5. No real technical answer — canned acknowledgements were sent
The reply path defaulted to a generic “thanks for getting back to me” you never
wrote, because fact grounding failed (finding 6).
**Fix:** `core/operations/composition.py::_structured_reply` rewritten as a
truthful, grounded reply: **no fabricated facts, no canned filler**, signature
wired from the adapter. If there is nothing real to say, the engine defers
rather than bluffing (finding 6/7).

### 6. Project observation produced `0 fact(s)`
The observer looked only directly in the configured directory, missing the real
project nested one level deeper (`…/Doug` wrapper → `…/Doug/IdentityOS` repo).
**Fix:** `core/operations/observer.py` auto-resolves a **nested project root**
(one–two-level `pyproject.toml` scan, unambiguous best-scoring child by
`.git`/`README`/tests), and only when it is unambiguous.

### 7. Facts were shallow strings with no provenance
Even a successful observation produced unverifiable claims.
**Fix:** observation now yields `ProjectState.fact_details: list[ProjectFact]`
with `source_type`/`source_path`/`observed_at`/`commit_sha`, drawn from
pyproject metadata, README, docs, registry config, tests, and git. `metadata`
carries the resolved root and commit SHA. CLI surface: `identity aster
status`, `would-send`, `notify`, `provenance` for review.

### 8. `$200,000 for 10%` was not escalated for approval
High-value offer passed the old categorical gating.
**Fix:** `core/operations/policy.py` defines **immutable baseline consequential
categories** (investment, equity, contracts, legal, money, credentials, access,
ip ownership, repository access, employment, exclusivity).
`ControlState.require_approval_categories` may only *add*; a baseline category
cannot be disabled. Baseline test: an empty config still escalates
`$200,000 for 10%` and `category="investment"`.

### 9. Classification ran before the money/deal-shape guard
If the model mis-labelled the intent, consequential signals were skipped.
**Fix:** a deterministic regex scan over the combined routing/category/content
runs **before** model classification: money (`$`, `€`, amounts), compound
money+deal-shape (`$200,000` + `10%`), equity/share/ownership/sale/acquisition
phrases, and extended commitment phrasing all force
`AWAITING_AUTHORIZATION`.

### 10. Escalated messages should never be auto-replied
Once authority is required, the loop must stop being chatty about money that is
not yours to discuss.
**Fix:** escalated inbound marks the relationship
`awaiting_human_authorization` and yields **no auto-reply**; authorization is
reached only via `identity aster escalate` + `decide` (or a trusted operator).

### 11. Observe mode was ambiguous about external transmissions
ObseRvE was loosely defined and could be interpreted as still sending
messages.
**Fix:** observe mode is now **zero external transmissions, period** — inbound
is still read, facts still gathered, but all outbound goes to the *drafts
sandbox* surfaced by `identity aster would-send`. Nothing egresses on a real
transport in observe mode.

### 12. Unknown senders were replied to even in observe mode
Politeness drafting happened before classification.
**Fix:** disposition runs first: an unknown sender is `quarantined` and no
draft is even composed, in observe *or* autonomous mode. In observe mode only
trusted inbound (a real conversation) produces a `would-send` draft.

### 13. Confidence=0 outreach fired after disqualification
The candidate was disqualified yet still reached the act phase.
**Fix:** `core/operations/evaluation.py` now applies the `source_confidence`
factor **only when > 0** (no regression to zero-confidence scores) and a
zero-confidence recommendation becomes `hold` **unless `test_candidate=true`**
— plus a defense-in-depth gate in `engine._phase_act` that refuses any
QUALIFIED candidate with `confidence == 0.0` and no `test_candidate` flag.

### 14. Principal notifications were noisy and un-deduplicated
Repeated ticks spammed similar gap messages.
**Fix:** `core/operations/capability_gap.py` exposes `CapabilityStatus`
(`ready` / `capability_missing` / `available_not_installed` /
`installed_permission_missing`) on `CapabilityGap`; principal notifications are
emitted with kind `permission_required` or `capability_available` and
**deduplicated by (kind, required_skill)** across ticks. Regression tests cover
both statuses and dedupe.

### 15. Historical inbox contents were treated as new conversation
First connect replayed the whole mailbox.
**Fix:** IMAP uses a persisted high-water mark (`fetch_inbox_with_cursor`):
UID/UIDNEXT cursor with UIDVALIDITY reseed; a first run **baselines without
ingesting**, so old mail is never re-processed; cursor is saved on every fetch
and tolerant of `MailboxCursor | dict` storage.

### 16. The suite now locks all of the above in
- `test_operations_engine.py` (42): automated sender ignored + no forging,
  unsolicited sender quarantined, thread-intrusion quarantine, zero-facts
  knowledge gate (deferred + `project_context_unavailable`), `$200k/10%`
  escalation, baseline-categories-can’t-be-disabled, confidence 0 hold vs
  `test_candidate` override, gap statuses + dedupe, nested-root observer,
  raw-body provenance through the ingest path.
- `test_email_capability.py` (19): FakeIMAP cursor baseline-without-replay,
  UIDVALIDITY reseed, UIDNEXT fallback to `UID SEARCH ALL`, message-literal
  extraction, `read_inbox_cursor` round-trip, MIME `multipart/alternative`
  plain-preference, HTML-only fallback, attachment-as-body ignored,
  quoted-reply stripping (English + Arabic markers), generated `Message-ID`
  send/persist round-trip.
- Observe-mode test updated to pre-seed a trusted relationship and asserts
  drafts-without-send; candidacy/mirror fixtures default trustworthy confidences.
- Targeted run + full suite verified from the live run itself (below).

### 17. Two real-server regressions the FakeIMAP could not catch
The `.aster-live-v2` live run immediately surfaced two defects in the cursor
path that the fake never exercised:
1. **Gmail does not echo `UIDNEXT` via `imap.response()` after SELECT**, so the
   cursor read `uid_validity=0, last_uid=0` and *reseeded on every tick* — no
   mail was ever ingested. Fixed with an explicit `STATUS (UIDVALIDITY UIDNEXT)`
   call plus a `UID SEARCH ALL` fallback.
2. **Real imaplib literal tuples are `(marker, message, flag)`**; the parser
   previously took the first bytes (`b"66 (RFC822 {740}"`) as the message and
   produced headerless, empty mail. Fixed by taking the **longest** bytes item.
Both are now locked in by regression tests (`test_imap_cursor_uidnext_falls_back_to_uid_search_all`,
`test_extract_raw_prefers_message_literal_over_rfc822_marker`).

### 18. Real Gmail replies arrived with empty/garbage bodies
The human answered the outreach thread from Gmail (two replies, UIDs 69–70).
The fetch path parsed with `get_payload()`, which returns `None`/wrong text on
`multipart/alternative` mail and includes full quoted history otherwise — so
inbound bodies were empty or quote-polluted, and the empty-body gate
(`inbound_body_unavailable`, defer + notification) fired instead of a reply.
**Fix:** `core/capabilities/email/backends.py` gains `extract_message_text()`
(multipart-aware: prefers `text/plain`, strips HTML tags from `text/html`,
ignores attachments as body) and `strip_quoted_reply()` (strips `On … wrote:`
and localized quote markers, e.g. Arabic `في … كتب:`), wired into **both** fetch
paths (`fetch_inbox` and `fetch_inbox_with_cursor`). `Message` now carries
`raw_body` + `body_sha256` so the audit trail proves what was actually received
while `body` holds only the new contribution used for classification.
Verified against the live mailbox: both Gmail replies extracted cleanly
(`'This is the second time you sent this but yes I have seen it'`,
`"Why isn't IdentityOS just a memory database wrapped around an LLM?"`).

### 19. Outbound messages had no durable external identity
Outbound mail carried no persisted RFC `Message-ID`, so threading and dedupe
had no anchor across restarts.
**Fix:** outbound sends generate a `Message-ID` (`generate_message_id()`),
persist it as the message's `external_id`, and dedupe inbound mail by
IMAP `UID`+`UIDVALIDITY` / RFC `Message-ID`.

### 20. Relationship updates were lost on reply
`_send_reply` mutated the relationship in memory without persisting, so
`outreach_sent` → later states were silently dropped (lost update).
**Fix:** `_send_reply` persists the relationship after mutation.

### 21. Canned answers can no longer masquerade as model replies
The facts-dump fallback in `OutreachComposer._structured_reply` is removed.
`compose_reply` now returns `(subject, body, generation_mode)` where
`generation_mode` is `identity_model_generation` (model-written — the only mode
allowed for substantive intents), `template_fallback` (status-only close:
decline / thanks / scheduling), or `unavailable` (substantive intent but no
working model → the monitor **defers**, never sends a canned answer). Outbound
messages record `generation` provenance. Consequential-signal scanning
(money/equity/commitment) now runs on the stripped new text.

---

## Pre-existing issues (not from this work)

Verified against a clean `git worktree` at HEAD **without** the working-tree
changes (identical symptoms on baseline code):

1. **Cross-app continuity `/process` fails with real credentials**
   (`tests/test_cross_app_continuity.py`): with the default adapter the chat
   request hangs (>90 s, no response); with `IDENTITY_ADAPTER=groq` the adapter
   chain is degraded — `GroqAdapter` dials `api.groq.com` with
   `model='phi4-mini:latest'` (an Ollama model name from
   `IDENTITY_ADAPTER_CONFIG`) → connection error, `CerebrasAdapter` keys are
   dead (`402 Payment required`), and the chain falls through to OpenRouter;
   some replies then leak raw `<thought>` reasoning tags into the output.
   This is configuration/adapter work (model names, key validity, request
   timeouts), deliberately left out of the email/operations scope of this
   round.
2. `test_cli_main.py` triggers the repo `.env` load at import time
   (`identitybench.cli` → `load_dotenv()`), which is why credential-gated tests
   run in full-suite runs but skip standalone.

---

## `.aster-live-v2` — permissioned re-run

New clean store. The old polluted store (`.aster-live`) is kept as evidence and
is **not** used.

### 0. Prerequisites
- Real credentials **must** be provided by you — this repo never contains them
  and no step below fabricates them.
- `ASTER_TEST_RECIPIENT` is a human-checked test recipient; the allowlist +
  candidates file are the enforcement boundary, not just this variable.

```sh
export IDENTITY_SMTP_HOST=smtp.gmail.com
export IDENTITY_SMTP_PORT=465
export IDENTITY_SMTP_USER='<address>@gmail.com'
export IDENTITY_SMTP_PASSWORD='<app password>'
export IDENTITY_IMAP_HOST=imap.gmail.com
export IDENTITY_IMAP_PORT=993
export IDENTITY_EMAIL_FROM='Aster <address>@gmail.com'
export ASTER_TEST_RECIPIENT='<your-check-address>@example.com'
# optional file mailbox instead of real IMAP for the read-only rehearsal:
# export IDENTITY_MAILBOX_ROOT=~/mailbox-v2
```

### 1. Clean-store init (grant email read + send)
```sh
python3 -m cli.main aster init \
  --store .aster-live-v2 \
  --project-root "$PWD" \
  --grant-email --grant-operations
```
> Note: `--grant-email` grants `email.send/read`; the inbox cursor fix (finding
> 15) is enabled by `IDENTITY_IMAP_HOST` being set at init for a real SMTP/IMAP
> backend.

### 2. Read-only rehearsal in observe mode (finding 11/12)
```sh
python3 -m cli.main aster outbound-mode --store .aster-live-v2 observe
python3 -m cli.main aster run --store .aster-live-v2 --once
python3 -m cli.main aster tick --store .aster-live-v2          # repeatable
python3 -m cli.main aster status --store .aster-live-v2
python3 -m cli.main aster would-send --store .aster-live-v2    # drafts only, nothing egresses
python3 -m cli.main aster notify --store .aster-live-v2        # incl. gap/permission kinds
python3 -m cli.main aster provenance --store .aster-live-v2    # dispositions shown per message
```
Expected: `0 fact(s)` is impossible (`status` shows real project facts from the
resolved nested root, finding 6/7); no automated/unknown mail is drafted; any
real thread stays a proper conversation.

### 3. Permissioned autonomous (still allowlisted, no outreach)
```sh
python3 -m cli.main aster override --store .aster-live-v2 allowed_external_recipients="<ASTER_TEST_RECIPIENT>"
python3 -m cli.main aster outbound-mode --store .aster-live-v2 autonomous
python3 -m cli.main aster tick --store .aster-live-v2
python3 -m cli.main aster messages --store .aster-live-v2 --unseen
python3 -m cli.main aster relationships --store .aster-live-v2
python3 -m cli.main aster escalate --store .aster-live-v2      # any \$/equity → here, no auto-reply
```
Deliberately **do not** seed or contact Beta Fund; no general outreach is
enabled. Anything consequential arrives in `escalate` for a human decision:
```sh
python3 -m cli.main aster decide --store .aster-live-v2 approve|reject <opportunity_id>
```

### 4. Verification checklist for the re-run
1. `provenance` shows the actual sender on `quarantined`/`ignored` entries.
2. No relationship ever contains a `no-reply@…` or an intruder address.
3. Every reply is grounded (no canned “thanks for getting back to me”).
4. `status` shows the resolved nested project root and non-zero facts.
5. A crafted `$200,000 for 10%` test email lands in `escalate`, not outbound.
6. Observe mode produced zero SMTP egress across multiple runs.

### Rollback
The store is a single directory; remove `.aster-live-v2` and re-init for a
from-scratch re-run. The evidence store `.aster-live` remains untouched.

---

## Live run results (2026-09-17)

Executed per the plan above on a clean `.aster-live-v2` store.

### Verified automatically
- `identity aster email-check`: SMTP (587, STARTTLS) **reachable, authenticated**;
  IMAP (993) **reachable, authenticated, INBOX selectable**.
- Clean-store init (json backend, `--grant-email --grant-operations`): live
  engine, SMTP/IMAP backend referenced from env, credentials never stored.
- Observe rehearsal: project resolved with **8 provenanced facts**
  (source_type/source_path/commit_sha), no `0 fact(s)`; first run baselined the
  mailbox without replaying history.
- IMAP cursor persisted with real `uid_validity`/`last_uid`; the
  `STATUS`/literal-extraction fixes (finding 17) landed after the run showed the
  reseed/empty-mail bugs.
- Self-copies (`arsenemnz@gmail.com → arsenemnz@gmail.com`): silently dropped
  at the transport boundary, cursor advanced, **no reply, no relationship**,
  no errors across repeated ticks.
- Allowlisted autonomous outreach: candidate → qualified → sent to
  `a.manzi@eagles.oc.edu` (SMTP evidence on the persisted message), transparent
  AI-operator disclosure + multi-line signature; relationship now
  `a.manzi@eagles.oc.edu → outreach_sent`.
- Full suite after the live-run fixes: **1122 passed / 40 skipped / 3
  env-gated errors** (+2 regression tests for the real-server cursor bugs).

### Still requires the human (by design)
- **Reply loop:** the human answered the outreach thread on 2026-09-18 (two
  Gmail replies, UIDs 69–70). Extraction was verified against the live mailbox
  (finding 18); the full tick processed them with `raw_body` + `body_sha256`
  provenance. End-to-end grounded reply generation now requires a working model
  adapter — with the repo's current degraded adapter chain (see "Pre-existing
  issues"), substantive replies **defer by design** (finding 21) instead of
  sending a canned answer. Fixing the adapter configuration (correct Groq model
  name in `IDENTITY_ADAPTER_CONFIG`, valid Cerebras/OpenRouter keys, bounded
  request timeouts) unblocks the full loop:
  ```sh
  python3 -m cli.main aster tick --store .aster-live-v2
  python3 -m cli.main aster provenance --store .aster-live-v2   # disposition + sender shown
  python3 -m cli.main aster relationships --store .aster-live-v2
  ```
  Expected: trusted-thread reply, model-generated grounded answer, sender named
  in provenance.
- **Escalation probe (optional):** a crafted email in the same thread such as
  “I'd like to offer $200,000 for 10%” should surface in
  `identity aster escalate` with **no auto-reply**.

Store: `.aster-live-v2` (gitignored as `.aster-live*`).