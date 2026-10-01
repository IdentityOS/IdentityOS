# Running Aster 24/7

Aster's identity is meant to outlive any one machine. This document covers the
two supported deployments and the guarantees each one gives you.

The distinction that shapes everything here:

> **The identity is portable. Its capabilities are not.**

A laptop-bound capability (driving a logged-in Firefox profile, a desktop app
bridge) can fail without Aster existing any less. So a laptop going to sleep
must never take the identity offline, and the health endpoint reports a missing
device bridge as a *subsystem* degradation, not as the identity being down.

---

## What actually has to survive

| State | Lives in | Lost if the machine dies? |
|---|---|---|
| Identity, memory, facts, relationships | identity store | **yes** — this is the identity |
| Durable email jobs and provenance | identity store | **yes** |
| Model / mail credentials | secret files | re-enterable, not identity |
| Installed capabilities | registry | rebuildable |
| Browser session | the human's laptop | expected to be gone |

If you deploy without a persistent volume, you get a process, not an identity.

---

## Option A: systemd on a small always-on Linux VM

Cheapest option that meets the requirement, and the recommended one.

```bash
# 1. Clone onto the VM
git clone <your fork> ~/Documents/Doug/IdentityOS
cd ~/Documents/Doug/IdentityOS
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 2. Create the data and secret directories
mkdir -p ~/.identityos

# 3. Install the units
cp deploy/systemd/aster-operator.service ~/.config/systemd/user/
cp deploy/systemd/aster-presence.service ~/.config/systemd/user/

# 4. Write the secrets (never commit these). The units load exactly these
# paths, and a missing file is NOT fatal (leading "-"), so a started unit does
# not prove it can send mail. Confirm with /api/health, not with `systemctl`.
$EDITOR ~/Documents/Doug/IdentityOS/.env     # IMAP/SMTP, Gmail app password
$EDITOR ~/Documents/Doug/.env                # model provider keys, test recipient
$EDITOR ~/Documents/Doug/IdentityOS/.gmail_push.env   # Gmail push OAuth (optional)

# 5. Enable and verify
systemctl --user daemon-reload
systemctl --user enable --now aster-operator.service aster-presence.service
loginctl enable-linger "$USER"    # survive logout and reboot

# 6. Confirm it is actually working
curl -s localhost:8787/ready
curl -s localhost:8787/api/health | python3 -m json.tool
systemctl --user status aster-operator.service
```

`ready` must return `"ready": true` and `/api/health` must not report
`identity_runtime: offline`. Anything else is a real failure, not a formality.

### Why these unit settings matter

- `Restart=always` — an OOM kill or an unhandled crash comes back. The
  pre-existing local unit said `on-failure`, which will not restart after a
  clean exit caused by, say, a config reload gone wrong.
- `TimeoutStopSec=120` — SIGTERM triggers a graceful drain. Without it, systemd
  sends SIGKILL at the default 90s and a reply in flight can be lost.
- `--email-interval 20` — mailbox ingest runs on its own short cadence,
  independent of the operator's 300s loop. With only one interval, a message
  waits behind operator work and the observed delay reached six minutes.
- `ProtectSystem=strict` + `ReadWritePaths` — the runtime can write only to its
  data directory. A compromised capability cannot repaint the host.
- `EnvironmentFile` — secrets are injected at run time, so they never enter an
  image layer, a commit, or a process listing.

### Reaching the dashboard

Bind stays on `127.0.0.1`. Reach it over a private overlay or an SSH tunnel:

```bash
ssh -L 8787:127.0.0.1:8787 user@vm
# then browse http://127.0.0.1:8787
```

or Tailscale Serve. **Do not** put this on a public interface: `/api/health`
and the communications API expose Aster's conversations, and the write endpoints
can steer her.

---

## Option B: Docker

```bash
docker build -f deploy/Dockerfile -t identityos-aster .

docker run -d --name aster \
  --restart unless-stopped \
  --init \
  -p 127.0.0.1:8787:8787 \
  -v "$HOME/Documents/Doug/IdentityOS/.identity_store:/data" \
  --env-file "$HOME/Documents/Doug/IdentityOS/.env" \
  --env-file "$HOME/Documents/Doug/.env" \
  --env-file "$HOME/Documents/Doug/IdentityOS/.gmail_push.env" \
  identityos-aster
```

The `HEALTHCHECK` hits `/ready`, which verifies the process answers *and* its
store is readable — a container with a failed volume mount reports unhealthy
rather than looking alive. `--init` reaps zombies; `STOPSIGNAL SIGTERM` reaches
PID 1 so the operator persists state before exiting.

---

## Event-driven Gmail ingest (primary path)

Polling stays available as a fallback, but a reply should not wait on an
interval. With a Gmail watch established, the mailbox itself becomes the
trigger:

```
Gmail users.watch  ->  Pub/Sub topic  ->  POST /api/gmail/push
                                              |  verify, durably queue, ack
                                              v
                                    Gmail history.list(startHistoryId)
                                              |
                                    Gmail messages.get  ->  durable EmailJob
```

The notification is a hint, not the payload. The endpoint acknowledges as soon
as the notification is on disk, and a worker does the Gmail reads — so a Gmail
outage cannot cause Pub/Sub to redeliver into a struggling endpoint, and a
crash between notification and processing loses nothing.

### Which process does what

The two processes have different jobs, and the split is deliberate:

| Process | Gmail push responsibility |
| --- | --- |
| `runtime.health_server` (presence) | Receives `POST /api/gmail/push`, verifies it, queues the notification durably, keeps the watch renewed, acks Pub/Sub. **Never ingests.** |
| `cli.main aster run` (operator) | Drains the queue: `history.list`, `messages.get`, and the same `ingest_messages` pipeline polling uses. **The only writer of email jobs and outbound sends.** |

The presence process has no configured engine — no transport, no adapter, no
sender address — so it could not answer mail even if it tried. More
importantly, two processes both creating jobs and sending replies is how a
person receives the same answer twice. The queue lives in shared storage, so
ownership is what splits, not visibility: if the operator is down the queue
accumulates and drains on restart. The mail is late, not lost, and
`/api/health` shows the depth.

A drain only advances its high-water mark when every message in the batch was
read *and* accepted by the pipeline. A partial failure holds the mark and the
queue and retries, because advancing past an unreadable message would drop it
permanently. After `MAX_DRAIN_ATTEMPTS` a permanently undeliverable
notification is abandoned — and counted as `abandoned_notifications`, which
forces `degraded` in health rather than looking healthy after losing mail.

### One-time setup

1. Create a Google Cloud project and enable the **Gmail API**.
2. Create an **OAuth client** (type: Web application). Add
   `https://gmail.googleapis.com/auth/gmail.readonly` and
   `https://gmail.googleapis.com/auth/pubsub`.
3. Create a **Pub/Sub topic** and grant the Gmail service account
   (`gmail-api-push@system.gserviceaccount.com`) **Publisher** on it.
4. Create a **push subscription** on that topic with the endpoint URL:

   ```
   https://<your-host>:8787/api/gmail/push?token=<ASTER_GMAIL_PUSH_VERIFICATION_TOKEN>
   ```

   Use an HTTPS endpoint in production. The endpoint binds to `127.0.0.1`, so
   put it behind a reverse proxy or a private tunnel rather than opening a port.
5. Run the OAuth consent flow once to obtain a refresh token, then write:

   ```bash
   cat > ~/Documents/Doug/IdentityOS/.gmail_push.env <<'EOF'
   ASTER_GMAIL_PUSH_CLIENT_ID=...
   ASTER_GMAIL_PUSH_CLIENT_SECRET=...
   ASTER_GMAIL_PUSH_REFRESH_TOKEN=...
   ASTER_GMAIL_PUSH_TOPIC=projects/<project>/topics/<topic>
   ASTER_GMAIL_PUSH_VERIFICATION_TOKEN=$(openssl rand -hex 32)
   EOF
   chmod 600 ~/Documents/Doug/IdentityOS/.gmail_push.env
   ```

6. Restart the presence service and confirm:

   ```bash
   systemctl --user restart aster-presence.service
   journalctl --user -u aster-presence.service -n 20
   curl -s localhost:8787/api/health | python3 -c \
     'import json,sys; print(json.load(sys.stdin)["gmail_push"])'
   ```

   `status` should become `healthy` with a non-empty `history_id`.

### What to check afterwards

- `notifications_received` must rise when mail arrives. If it does not, the
  Pub/Sub subscription is not reaching the endpoint.
- `history_lookups` and `messages_ingested` must rise with it. Notifications
  arriving but messages staying at zero means Gmail history is being read and
  returning nothing — check the label filter.
- `expiration` must stay in the future. It refreshes on its own daily; a
  lapsed watch reports `offline` rather than looking fine.
- `queue_depth` should return to zero. A queue that only grows means the drain
  worker is not running.

Nothing here is required. With no `gmail_push.env`, the service starts
normally, `/api/health` reports `gmail_push: not_configured`, and IMAP polling
continues to work.

---

## Backups

The store is a directory of JSON. Copy it while the service is stopped, or use
the backend's own snapshot support:

```bash
systemctl --user stop aster-operator.service aster-presence.service
tar czf ~/aster-backup-$(date +%F).tar.gz -C ~/Documents/Doug/IdentityOS .identity_store
systemctl --user start aster-operator.service aster-presence.service
```

Back up the store, not the secrets, and store backups with the same care as the
original — they contain the principal's private correspondence.

Verify a restore by starting a *fresh* process against the copy and confirming
the identity, relationships, and job ledger are present. An untested backup is
not a backup.

---

## Rollback

```bash
git -C ~/identityos log --oneline -5      # find the last good commit
git -C ~/identityos checkout <commit>
systemctl --user restart aster-operator.service
curl -s localhost:8787/api/health
```

The store format is additive. New optional fields default to empty when a
record lacks them, so rolling back the code does not corrupt a store written by
a newer version.

---

## Verifying the deployment is honest

The point of the health endpoint is that it cannot be satisfied by the process
merely being up. Check each of these against real behavior:

1. `/ready` returns `ready: true` after a restart — state survived.
2. `/api/health` shows `identity_runtime: healthy` with a recent heartbeat age.
3. `email_ingest` is `healthy` with a recent poll, or explains why it is not.
4. `email_sender` shows `awaiting_response: []` when nothing is owed, and lists
   held/failed jobs when something is.
5. Kill the operator process. `Restart=always` brings it back and the heartbeat
   age never exceeds the stale threshold without `/api/health` saying `offline`.

If any of these can be made to look healthy while the system is actually
broken, that is a bug in the health code, not a deployment problem.
