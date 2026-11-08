"""Private SQLite intent mailbox. Only a source owner may acknowledge settlement.

The caller supplies an explicit profile-scoped path; no environment or config lookup.
Responder mappings must be authenticated by the transport before entering this module.
The local OS account is trusted: owner_id is an unguessable per-owner epoch capability,
not a session id. This database is never a browser API response.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import sqlite3
import time

TERMINAL = frozenset({"submitted", "cancelled", "timed_out", "undelivered"})
SCOPE = ("user_id", "chat_id", "thread_id", "bot_id")
CHOICES = frozenset({"once", "deny", "session", "always"})


class Conflict(ValueError):
    status_code = 409


class Expired(ValueError):
    status_code = 410


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def binding_digest(row):
    """Immutable source identity; retained privately by the owner adapter."""
    fields = ("handle", "kind", "payload", "owner_id", "request_id", "recipient", "expires_at", "allowed_choices")
    return hashlib.sha256(_json({k: row[k] for k in fields}).encode()).hexdigest()


def _scope(value):
    if not isinstance(value, dict) or any(k not in value for k in SCOPE):
        raise ValueError("complete recipient scope required")
    if any(value[k] is None or isinstance(value[k], bool) for k in ("user_id", "chat_id", "bot_id")):
        raise ValueError("recipient user/chat/bot required")
    return {k: value[k] for k in SCOPE}


class Mailbox:
    """One connection per transaction; safe across threads and independent processes."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Do not chmod a caller-owned shared directory. Require a private one instead.
        if os.name == "posix" and self.path.parent.stat().st_mode & 0o077:
            raise ValueError("mailbox directory must be private (0700)")
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.path, flags, 0o600)
        try:
            if os.name == "posix" and os.fstat(fd).st_mode & 0o077:
                raise ValueError("mailbox must be private (0600)")
        finally:
            os.close(fd)
        with self._transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS requests (handle TEXT PRIMARY KEY, body TEXT NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS requests_owner_status ON requests "
                       "(json_extract(body, '$.owner_id'), json_extract(body, '$.status'))")

    @contextmanager
    def _transaction(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("PRAGMA secure_delete=ON")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def _reader(self):
        db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
        try:
            yield db
        finally:
            db.close()

    @staticmethod
    def _read(db, handle):
        found = db.execute("SELECT body FROM requests WHERE handle=?", (handle,)).fetchone()
        return json.loads(found[0]) if found else None

    @staticmethod
    def _save(db, row):
        db.execute("UPDATE requests SET body=? WHERE handle=?", (_json(row), row["handle"]))

    @staticmethod
    def _expired(row, now=None):
        return row["expires_at"] is not None and (time.time() if now is None else now) >= row["expires_at"]

    def publish(self, *, kind, payload, owner_id, request_id, recipient, expires_at=None,
                delay_seconds=120, allowed_choices=None):
        if kind not in {"ask", "clarify", "approval"}:
            raise ValueError("unsafe or unsupported request kind")
        if not isinstance(payload, dict) or not owner_id or not request_id:
            raise ValueError("payload and exact owner/request required")
        if not math.isfinite(delay_seconds) or delay_seconds < 0:
            raise ValueError("invalid delay")
        if expires_at is not None and not math.isfinite(expires_at):
            raise ValueError("invalid expiry")
        choices = list(allowed_choices or [])
        if kind == "approval" and (not choices or not set(choices) <= CHOICES):
            raise ValueError("explicit approval choices required")
        now = time.time()
        handle = secrets.token_urlsafe(24)
        row = dict(version=1, handle=handle, kind=kind, title=str(payload.get("title", kind.title())),
                   payload=payload, owner_id=owner_id, request_id=request_id, recipient=_scope(recipient),
                   expires_at=expires_at, due_at=now + delay_seconds, created_at=now,
                   allowed_choices=choices, status="pending", revision=0, draft={}, result=None,
                   response=None, responder=None, delivery=None)
        with self._transaction() as db:
            db.execute("INSERT INTO requests VALUES (?, ?)", (handle, _json(row)))
        return handle

    def get(self, handle):
        with self._reader() as db:
            return self._read(db, handle)

    def _rows(self, db):
        return [json.loads(r[0]) for r in db.execute("SELECT body FROM requests ORDER BY rowid")]

    def ready(self, now=None):
        now = time.time() if now is None else now
        with self._transaction() as db:
            return [r for r in self._rows(db) if r["status"] == "pending" and r["delivery"] is None
                    and not r.get("delivery_attempted_at") and r["due_at"] <= now and not self._expired(r, now)]

    def claim_delivery(self, handle):
        """Persist the at-most-once network attempt BEFORE sending (crash may lose delivery)."""
        with self._transaction() as db:
            row = self._read(db, handle)
            now = time.time()
            if (not row or row["status"] != "pending" or row["delivery"] is not None
                    or row.get("delivery_attempted_at") or row["due_at"] > now or self._expired(row, now)):
                return False
            row["delivery_attempted_at"] = now
            self._save(db, row)
            return True

    def delivered(self, *, bot_id=None, limit=1000):
        """Private bounded presenter recovery rows, newest first (optional bot scope)."""
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise ValueError("delivery recovery limit must be 1..10000")
        with self._transaction() as db:
            result = []
            for (body,) in db.execute("SELECT body FROM requests ORDER BY rowid DESC"):
                row = json.loads(body)
                if row["delivery"] is not None and (bot_id is None or row["recipient"]["bot_id"] == bot_id):
                    result.append(row)
                    if len(result) == limit:
                        break
            return result

    def propose_side(self, handle, question_id, question, responder):
        if not isinstance(question, str) or not question.strip() or len(question) > 2000:
            return False
        try:
            responder = _scope(responder)
        except ValueError:
            return False
        with self._transaction() as db:
            row = self._read(db, handle)
            if (not row or row["status"] != "pending" or self._expired(row)
                    or responder != row["recipient"] or row["kind"] == "approval"):
                return False
            questions = row["payload"].get("questions", [])
            if not any(str(q.get("id", q.get("qid", ""))) == str(question_id) for q in questions):
                return False
            sides = row.setdefault("sides", [])
            if len(sides) >= 8 or any(s["status"] == "pending" for s in sides):
                return False
            side_id = secrets.token_urlsafe(24)
            sides.append(dict(side_id=side_id, question_id=question_id, question=question.strip(),
                              status="pending", answer=None))
            self._save(db, row)
            return side_id

    def side_requests(self, owner_id):
        with self._reader() as db:
            rows = db.execute("SELECT body FROM requests WHERE json_extract(body, '$.owner_id')=? "
                              "AND json_extract(body, '$.status')='pending' ORDER BY rowid", (owner_id,))
            return [dict(s, handle=r["handle"]) for (body,) in rows for r in [json.loads(body)]
                    if not self._expired(r) for s in r.get("sides", []) if s["status"] == "pending"]

    def acknowledge_side(self, handle, side_id, owner_id, answer):
        if not isinstance(answer, str) or len(answer) > 16000:
            return False
        with self._transaction() as db:
            row = self._read(db, handle)
            if (not row or row["owner_id"] != owner_id or row["status"] != "pending" or self._expired(row)):
                return False
            side = next((s for s in row.get("sides", []) if s["side_id"] == side_id), None)
            if not side or side["status"] != "pending":
                return False
            side.update(status="answered", answer=answer)
            self._save(db, row)
            return True

    def get_sides(self, handle):
        with self._transaction() as db:
            row = self._read(db, handle)
            if not row:
                return []
            return [dict(id=s["side_id"], **{k: s[k] for k in ("question_id", "question", "status", "answer")})
                    for s in row.get("sides", [])]

    def mark_delivered(self, handle, *, chat_id, message_id, thread_id=None):
        with self._transaction() as db:
            row = self._read(db, handle)
            if (not row or row["status"] != "pending" or row["delivery"] is not None or self._expired(row)
                    or chat_id != row["recipient"]["chat_id"] or thread_id != row["recipient"]["thread_id"]):
                return False
            row["delivery"] = dict(chat_id=chat_id, message_id=message_id, thread_id=thread_id)
            self._save(db, row)
            return True

    def propose(self, handle, *, response, responder, revision=None):
        if revision is not None and (type(revision) is not int or revision < 0):
            return False
        try:
            responder = _scope(responder)
        except ValueError:
            return False
        if not isinstance(response, dict):
            return False
        with self._transaction() as db:
            row = self._read(db, handle)
            if (not row or row["status"] != "pending" or self._expired(row) or responder != row["recipient"]
                    or (revision is not None and revision != row["revision"])):
                return False
            if row["kind"] == "approval" and (set(response) != {"choice"} or response["choice"] not in row["allowed_choices"]):
                return False
            row.update(status="submitting", response=response, responder=responder, revision=row["revision"] + 1)
            self._save(db, row)
            return True

    def intents(self, owner_id):
        with self._transaction() as db:
            return [r for r in self._rows(db) if r["owner_id"] == owner_id and r["status"] == "submitting"]

    def acknowledge(self, handle, *, owner_id, status, result=None):
        return self._acknowledge(handle, owner_id=owner_id, status=status, result=result)

    def _acknowledge(self, handle, *, owner_id, status, result=None, accepted_at=None, source_settled=False):
        # accepted_at is an owner-only receipt timestamp, never a transport field.
        if status not in TERMINAL:
            raise ValueError("owner acknowledgement must be terminal")
        with self._transaction() as db:
            row = self._read(db, handle)
            if not row or row["owner_id"] != owner_id or row["status"] in TERMINAL:
                return False
            if not source_settled and status == "submitted" and self._expired(row, accepted_at):
                return False
            row.update(status=status, result=result, revision=row["revision"] + 1)
            self._save(db, row)
            return True

    def update_draft(self, handle, *, draft, revision):
        if type(revision) is not int or revision < 0:
            raise Conflict("integer revision required")
        if not isinstance(draft, dict):
            raise ValueError("draft must be an object")
        with self._transaction() as db:
            row = self._read(db, handle)
            if not row:
                raise KeyError(handle)
            if self._expired(row):
                raise Expired("request expired")
            if row["status"] != "pending" or row["revision"] != revision:
                raise Conflict("stale revision or settled request")
            row.update(draft=draft, revision=row["revision"] + 1)
            self._save(db, row)
            return row
