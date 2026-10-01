# Aster Audit Checklist (internal)

Verify every claim from code, not from the earlier report.
Status: `PASS` = verified by reading code + running tests/CLI.
`PARTIAL` = works but missing a required property.
`FAIL` = required behavior absent.

## Core claims

| # | Claim | Status | Evidence |
|---|-------|--------|----------|
| 1 | Engine resumes after process restart | PASS | `OperationsStore.__init__` reloads all collections from storage; `test_restart_survives_and_prevents_resend` |
| 2 | No duplicate cold outreach per target | PASS | `DuplicateContactPolicy.evaluate` + `OpportunityDiscoverer.discover`; `test_second_tick_does_not_duplicate_outreach` |
| 3 | Capability gating on sends | PASS | `CapabilityTransport` calls `email.send` through registry; gate fails closed |
| 4 | Outreach is individualized from evidence | PASS | `OutreachBrief.from_opportunity`; body includes relevant_work/fit_reason; `test_tick_..._sends_outreach` |
| 5 | "Sent" is runtime-verified | PASS | Files written by `FileMailboxBackend.send`; SMTP raises on failure; FAILED records on failure paths |
| 6 | Escalation for consequential requests (outbound) | PASS | `AuthorityPolicy.evaluate(mode="commitment")`; `test_require_approval_category_escalates_and_authorize_sends` |
| 7 | Escalation for sensitive inbound requests | PASS | `AuthorityPolicy.evaluate(mode="content")`; `test_sensitive_inbound_request_is_escalated` |
| 8 | Opt-out honored everywhere | PASS | `_OPT_OUT_PATTERNS`; relationship.opted_out gates duplicates + follow-ups |
| 9 | Follow-up limited | PASS | planner max + budget check; `test_follow_up_respects_maximum` |
| 10 | Provenance ledger append-only, evidence-carrying | PASS | `append_provenance`; evaluate/escalate/authorize refs entries exist |
| 11 | Persistence across process | PASS | json/sqlite backend; full suite restart test |
| 12 | Capability registry conformance intact | PASS | `tests/test_capability_conformance.py` passes (19 entries) |

## Required behavior that is missing / partial

| # | Requirement (new task) | Status | Evidence |
|---|------------------------|--------|----------|
| 13 | `outbound_mode` (observe/autonomous/approval_required), persisted + CLI | PASS | `ControlState.outbound_mode` + `OUTBOUND_MODES`/`normalize_outbound_mode`; persisted via to_dict/from_dict; `aster outbound-mode` command; observe does not transmit, does not consume budget |
| 14 | Default mode conservative (observe) | PASS | `outbound_mode="observe"` default; `test_default_outbound_mode_is_observe` |
| 15 | WOULD_SEND records in observe mode + CLI listing | PASS | `MessageStatus.WOULD_SEND` + `would_send()`/`aster would-send`; observe branch in `_phase_act`/`_phase_follow_ups`; `test_observe_mode_records_would_send_without_transmitting` |
| 16 | Recipient allowlist `allowed_external_recipients` | PASS | field + `_allowlist_allows` (exact or `@domain`); gates cold outreach with reason; `test_allowlist_gates_cold_outreach`, `test_allowlist_domain_suffix_matches` |
| 17 | Replies grounded in VERIFIED project facts | PASS | `monitor._verified_facts(store)` reads `store.project_state().facts` and passes into `compose_reply` (decline + conversational) |
| 18 | Real SMTP/IMAP config from env, never persisted | PASS | transport built from env at construction; only non-secret `{"backend":"smtp"}` config reference persisted by `aster init`; `aster email-check` validates and never prints credentials |
| 19 | Inbound dedupe across ticks | PASS | `_already_processed` via `external_id` |
| 20 | Message-ID / In-Reply-To / References correlation | PASS | `parse_message_ids` (multi-ID); SMTP headers built from in_reply_to+references (refs[0] thread fallback); `Message.in_reply_to`/`references` persisted; `_resolve_relationship` matches any reference id in thread_ids; full threading plumbing through engine→monitor→transport |
| 21 | Bounce/auto-reply filtering | PASS | `SMTPBackend._ignored`: self-copy, bounce subjects, multipart/report+delivery-status, Auto-Submitted != no, Precedence auto_reply/bulk/list, X-Autoreply/X-Auto-Response-Suppress, List-* |
| 22 | Daemon singleton + stale pid recovery | PASS | flock-based lock file (spawn-window atomicity) + pidfile liveness guard (steady-state) + stale cleanup; double-spawn refused via CLI (verified) |
| 23 | Graceful shutdown persists state | PASS | SIGTERM handler requests stop between ticks; in-flight tick completes and state persists before exit; `stop` waits for pidfile removal (grace) with SIGKILL fallback; verified via daemon smoke test |
| 24 | Scoped, replay-proof authorization record | PASS | authorize() only accepts AWAITING_AUTHORIZATION (status transition = one-time guard; replay refused — verified); audit entry records decision/approver/timestamp/scope (relationship, opportunity, need, outbound_mode) and the authorization token; `decide --as <approver>` |
| 25 | Notify the principal on escalation | PASS | durable `NotificationEntry` ledger (operations.notifications), raised on every escalation (cold outreach, follow-ups, inbound reply approvals) + on authorize resolution; `aster notify [--all|--mark-read]`; `status.notifications.unread`; survival across restart tested |
| 26 | Observation-mode discovery (funding/compute/collaborators/…) | PASS | discovery + evaluation run every tick regardless of outbound mode; observe mode records WOULD_SEND only and transmits zero (verified: fresh observe tick → 0 outbox messages, all `would_send`); `aster opportunities` + `aster would-send` surface the observation stream; no Beta Fund (or any synthetic fund) injection anywhere in code/seed/demo — grep the whole repo shows it only inside `docs/ASTER-OPERATOR.md` as a documentation example |
| 27 | Conservative rate-limit defaults | PASS | `max_cold_outreach_per_day=3` default (was 5), follow-ups 2, follow-up delay 72h; budget check only in autonomous path; observe never consumes budget |
| 28 | RFC 5322 threading headers on replies | PASS | SMTP sets In-Reply-To/References (refs = references + in_reply_to at front, refs[0] thread fallback); file backend carries in_reply_to/references; round-trip test |
| 29 | `email-check` validation command | PASS | `aster email-check` (+ optional `--send-test`); returns JSON of SMTP/IMAP probes; never prints credentials; verified via CLI |
| 30 | Full-suite health | PASS | 1103 passed / 40 skipped / 3 errors — all 3 errors are pre-existing env-gated `test_cross_app_continuity.py:65` (`repo_root` NameError), unrelated |

## External environment

| # | Item | Status | Evidence |
|---|------|--------|----------|
| 31 | SMTP/IMAP credentials configured | NO | `env | grep -iE 'smtp|imap|aster_test|test_recipient|identity_(email|mail|smtp|imap)'` produced no output |
| 32 | Live external loop runnable | NO | requires credentials; Phase 14 boundary applies |

## Priority fixes (drive remaining phases)

1. ~~`outbound_mode` (default `observe`) + WOULD_SEND + gates + CLI + allowlist.~~ DONE (Phase 2)
2. ~~Wire verified project facts into autonomous replies.~~ DONE (item 17)
3. ~~Conservative defaults + threading + bounce filtering + `email-check`.~~ DONE (items 20-21, 27-29)
4. ~~Daemon hardening (flock + pidfile liveness + graceful in-flight stop).~~ DONE (items 22-23)
5. ~~Scoped replay-proof authorization + notification ledger + `aster notify`.~~ DONE (items 24-25)
6. ~~Observation-mode discovery — no injection, verified observe transmits zero.~~ DONE (item 26)

Remaining: external live-loop verification (blocked on credentials, items 31-32) and the final 18-item honest report.