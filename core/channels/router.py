"""SQLite switchboard state, deliberately separate from canonical identity state."""

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .context import AuthLevel


@dataclass(frozen=True)
class Route:
    identity_id: str
    session_id: str
    user_id: str
    channel: str
    auth_level: AuthLevel


class IdentityRouter:
    def __init__(self, path, identities):
        self.path = str(path)
        self.identities = dict(identities)  # stable alias -> canonical identity id
        if (
            not 1 <= len(self.identities) <= 9
            or len(set(self.identities.values())) != len(self.identities)
            or any(not re.fullmatch(r"[a-z0-9_-]+", a) for a in self.identities)
        ):
            raise ValueError("Configure 1–9 unique lowercase aliases pointing to distinct identities")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS callers (
                  caller TEXT PRIMARY KEY, user_id TEXT NOT NULL, preferred TEXT,
                  sms TEXT, salt BLOB, pin_hash BLOB, failures INTEGER DEFAULT 0,
                  locked_until REAL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS sessions (
                  user_id TEXT, channel TEXT, scope TEXT, identity_id TEXT, session_id TEXT,
                  PRIMARY KEY(user_id, channel, scope, identity_id));
                CREATE TABLE IF NOT EXISTS providers (name TEXT PRIMARY KEY, config_ref TEXT);
            """)

    @contextmanager
    def db(self):
        # Context manager closes connections as well as committing transactions.
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def bind(self, caller, user_id, default=None):
        if not caller or not user_id:
            raise ValueError("Caller and user are required")
        if default is not None:
            self.identity(default)
        with self.db() as db:
            old = db.execute("SELECT user_id FROM callers WHERE caller=?", (caller,)).fetchone()
            if old and old["user_id"] != user_id:
                raise ValueError("Caller is already bound to another user")
            db.execute(
                "INSERT INTO callers(caller,user_id,preferred) VALUES(?,?,?) "
                "ON CONFLICT(caller) DO UPDATE SET preferred=COALESCE(excluded.preferred,callers.preferred)",
                (caller, user_id, default),
            )

    def caller(self, caller):
        with self.db() as db:
            row = db.execute("SELECT * FROM callers WHERE caller=?", (caller,)).fetchone()
        if row is None:
            raise PermissionError("Unknown caller; enroll locally before calling")
        return dict(row)

    def identity(self, alias):
        try:
            return self.identities[alias.casefold()]
        except KeyError:
            raise ValueError("Unknown identity alias") from None

    def default(self, user, alias):
        self.identity(alias)
        with self.db() as db:
            if not db.execute("UPDATE callers SET preferred=? WHERE user_id=?", (alias, user)).rowcount:
                raise ValueError("Unknown user")

    def select_sms(self, caller, alias):
        self.identity(alias)
        self.caller(caller)
        with self.db() as db:
            db.execute("UPDATE callers SET sms=? WHERE caller=?", (alias, caller))

    def resolve(
        self, caller, channel, requested_identity=None, *, scope="default", auth_level=AuthLevel.RECOGNIZED, new=False
    ):
        row = self.caller(caller)
        alias = requested_identity or (row["sms"] if channel == "sms" else None) or row["preferred"]
        if not alias:
            raise ValueError("Choose an identity first")
        identity_id = self.identity(alias)
        key = (row["user_id"], channel, scope, identity_id)
        with self.db() as db:
            if new:
                db.execute("DELETE FROM sessions WHERE user_id=? AND channel=? AND scope=? AND identity_id=?", key)
            db.execute("INSERT OR IGNORE INTO sessions VALUES(?,?,?,?,?)", (*key, str(uuid.uuid4())))
            sid = db.execute(
                "SELECT session_id FROM sessions WHERE user_id=? AND channel=? AND scope=? AND identity_id=?", key
            ).fetchone()[0]
        return Route(identity_id, sid, row["user_id"], channel, auth_level)

    def end_call(self, scope):
        with self.db() as db:
            db.execute("DELETE FROM sessions WHERE channel='voice' AND scope=?", (scope,))

    def set_pin(self, user, pin):
        if not re.fullmatch(r"\d{6,12}", pin):
            raise ValueError("PIN must contain 6–12 digits")
        salt = secrets.token_bytes(16)
        digest = hashlib.scrypt(pin.encode(), salt=salt, n=16384, r=8, p=1)
        with self.db() as db:
            if not db.execute(
                "UPDATE callers SET salt=?,pin_hash=?,failures=0,locked_until=0 WHERE user_id=?", (salt, digest, user)
            ).rowcount:
                raise ValueError("Unknown user")

    def verify_pin(self, caller, pin):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM callers WHERE caller=?", (caller,)).fetchone()
            if row is None or row["pin_hash"] is None or row["locked_until"] > time.time():
                return False
            valid_format = bool(re.fullmatch(r"\d{6,12}", pin))
            digest = hashlib.scrypt((pin if valid_format else "").encode(), salt=row["salt"], n=16384, r=8, p=1)
            valid = valid_format and hmac.compare_digest(digest, row["pin_hash"])
            failures = 0 if valid else row["failures"] + 1
            locked = time.time() + 300 if failures >= 5 else 0
            db.execute("UPDATE callers SET failures=?,locked_until=? WHERE caller=?", (failures, locked, caller))
        return valid

    def callers(self):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT caller,user_id,preferred,sms FROM callers ORDER BY caller")]
