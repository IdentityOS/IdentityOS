"""Transactional local service agreements. Host-owned store, never model accessible.

SQLite serializes cross-identity credit reservations and completion. Model output
cannot write evidence, acceptance, grants, or balances. No network identity protocol
is implied: authentication is the trusted runtime binding, not a payload sender ID.
"""

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def uid():
    return uuid.uuid4().hex


class ServiceStore:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS services(identity TEXT PRIMARY KEY, document TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS negotiations(id TEXT PRIMARY KEY, dedupe TEXT UNIQUE NOT NULL,
                    requester TEXT NOT NULL, provider TEXT NOT NULL, revision INTEGER NOT NULL,
                    state TEXT NOT NULL, contract TEXT NOT NULL, initial_hash TEXT NOT NULL,
                    offered_by TEXT NOT NULL, job TEXT, reason TEXT NOT NULL,
                    created REAL NOT NULL, updated REAL NOT NULL, authority TEXT NOT NULL);
                CREATE TRIGGER IF NOT EXISTS negotiation_participants BEFORE UPDATE OF requester,provider,initial_hash,dedupe ON negotiations
                    BEGIN SELECT RAISE(ABORT,'immutable participants'); END;
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, dedupe TEXT UNIQUE NOT NULL,
                    requester TEXT NOT NULL, provider TEXT NOT NULL, state TEXT NOT NULL,
                    contract TEXT NOT NULL, price INTEGER NOT NULL DEFAULT 0,
                    reason TEXT NOT NULL DEFAULT '', artifact TEXT, task TEXT,
                    created REAL NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY, sender TEXT NOT NULL,
                    recipient TEXT NOT NULL, thread TEXT NOT NULL, reply_to TEXT,
                    kind TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS accounts(identity TEXT PRIMARY KEY, spending_limit INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS transactions(id TEXT PRIMARY KEY, job TEXT UNIQUE,
                    reason TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS entries(tx TEXT NOT NULL, account TEXT NOT NULL, amount INTEGER NOT NULL,
                    PRIMARY KEY(tx, account), FOREIGN KEY(tx) REFERENCES transactions(id));
                CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    identity TEXT NOT NULL, job TEXT, kind TEXT NOT NULL, data TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS artifacts(hash TEXT PRIMARY KEY, document TEXT NOT NULL);
                CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'append only'); END;
                CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'append only'); END;
                CREATE TRIGGER IF NOT EXISTS entries_no_update BEFORE UPDATE ON entries BEGIN SELECT RAISE(ABORT,'append only'); END;
                CREATE TRIGGER IF NOT EXISTS entries_no_delete BEFORE DELETE ON entries BEGIN SELECT RAISE(ABORT,'append only'); END;
                CREATE TRIGGER IF NOT EXISTS tx_no_update BEFORE UPDATE ON transactions BEGIN SELECT RAISE(ABORT,'append only'); END;
                CREATE TRIGGER IF NOT EXISTS tx_no_delete BEFORE DELETE ON transactions BEGIN SELECT RAISE(ABORT,'append only'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_contract BEFORE UPDATE OF contract,requester,provider ON jobs
                    BEGIN SELECT RAISE(ABORT,'immutable agreement'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_quote BEFORE UPDATE OF price,reason ON jobs
                    WHEN OLD.state != 'REQUESTED' BEGIN SELECT RAISE(ABORT,'immutable quote'); END;
            """)

        Path(self.path).chmod(0o600)

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def rows(self, sql, params=()):
        with sqlite3.connect(self.path, timeout=10) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(sql, params)]

    def event(self, db, identity, kind, data, job=None):
        db.execute(
            "INSERT INTO events(identity,job,kind,data,created) VALUES(?,?,?,?,?)",
            (identity, job, kind, encode(data), time.time()),
        )

    def job(self, job_id):
        rows = self.rows("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not rows:
            raise ValueError("unknown job")
        result = rows[0]
        result["contract"] = json.loads(result["contract"])
        return result

    def jobs(self, identity):
        return [
            self.job(r["id"])
            for r in self.rows(
                "SELECT id FROM jobs WHERE requester=? OR provider=? ORDER BY created", (identity, identity)
            )
        ]

    @staticmethod
    def balance_in(db, identity):
        return db.execute("SELECT COALESCE(SUM(amount),0) FROM entries WHERE account=?", (identity,)).fetchone()[0]

    def balance(self, identity):
        with self.transaction() as db:
            return self.balance_in(db, identity)

    def reputation(self, identity):
        jobs = self.rows("SELECT state,requester FROM jobs WHERE provider=?", (identity,))
        events = self.rows("SELECT kind FROM events WHERE identity=?", (identity,))
        customers = [j["requester"] for j in jobs if j["state"] == "COMPLETED"]
        return {
            "completed_jobs": len(customers),
            "failed_jobs": sum(j["state"] == "FAILED" for j in jobs),
            "acceptance_passes": sum(e["kind"] == "acceptance_passed" for e in events),
            "acceptance_failures": sum(e["kind"] == "acceptance_failed" for e in events),
            "rework": sum(e["kind"] == "rework_requested" for e in events),
            "repeat_customers": sum(customers.count(c) > 1 for c in set(customers)),
        }

    def bootstrap(self, identity, amount, *, allocation_id):
        """Trusted operator only. No identity-facing mint or spending-limit setter."""
        if type(amount) is not int or amount <= 0 or not allocation_id:
            raise ValueError("positive integer allocation and provenance required")
        with self.transaction() as db:
            db.execute("INSERT OR IGNORE INTO accounts(identity) VALUES(?)", (identity,))
            tx = "bootstrap:" + allocation_id
            db.execute("INSERT INTO transactions VALUES(?,NULL,?,?)", (tx, "SYSTEM_BOOTSTRAP", time.time()))
            db.executemany("INSERT INTO entries VALUES(?,?,?)", [(tx, "@system", -amount), (tx, identity, amount)])
            self.event(db, identity, "bootstrap", {"transaction": tx, "amount": amount})

    def set_spending_limit(self, identity, amount, *, principal_reference):
        if type(amount) is not int or amount < 0 or not principal_reference:
            raise ValueError("principal decision required")
        with self.transaction() as db:
            db.execute(
                "INSERT INTO accounts VALUES(?,?) ON CONFLICT(identity) DO UPDATE SET spending_limit=excluded.spending_limit",
                (identity, amount),
            )
            self.event(
                db, identity, "economic_authority", {"limit": amount, "principal_reference": principal_reference}
            )
