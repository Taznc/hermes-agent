"""Source-process adapters to existing resolver primitives, not replacement registries.

Construct one bridge per live owner/profile generation. Never reconstruct an old
owner epoch after process restart; orphaned handles cannot settle a new request.
"""
from __future__ import annotations

from dataclasses import dataclass
import secrets
import threading
import time

from .mailbox import TERMINAL, binding_digest


@dataclass
class _Binding:
    digest: str
    snapshot: dict
    current: object
    resolve: object
    deferred_ack: bool = False
    awaiting_ack: bool = False
    accepted_at: float | None = None


class OwnerBridge:
    def __init__(self, mailbox, recipient):
        self.mailbox = mailbox
        self.recipient = dict(recipient)
        self.owner_id = secrets.token_urlsafe(32)
        self._bindings = {}
        self._lock = threading.RLock()
        self._closed = False

    def close(self):
        """Invalidate unsatisfied projections at owner teardown; do not cancel a source receipt."""
        with self._lock:
            self._closed = True
            for handle, binding in list(self._bindings.items()):
                if not binding.awaiting_ack:
                    self.settle(handle, "cancelled")

    def _watch(self, request_id, *, kind, payload, current, resolve, expires_at=None,
               delay_seconds=120, allowed_choices=None, deferred_ack=False):
        with self._lock:
            if self._closed:
                raise ValueError("owner epoch is closed")
            snapshot = current()
            if snapshot is None:
                raise ValueError("source request is not pending in this owner")
            handle = self.mailbox.publish(kind=kind, payload=payload, owner_id=self.owner_id,
                                          request_id=request_id, recipient=self.recipient,
                                          expires_at=expires_at, delay_seconds=delay_seconds,
                                          allowed_choices=allowed_choices)
            self._bindings[handle] = _Binding(binding_digest(self.mailbox.get(handle)), snapshot,
                                              current, resolve, deferred_ack)
            return handle

    @staticmethod
    def _find(snapshots, request_id):
        return next((r for r in snapshots if r["id"] == request_id), None)

    @staticmethod
    def _check_request(frame, kind, payload):
        if frame is None:
            raise ValueError("unknown source request")
        params = frame.get("params", {})
        valid = (kind == "ask" and frame["method"] == "plugin.request"
                 and params.get("kind") == "fork-ask/questions" and params.get("payload") == payload)
        if kind == "clarify":
            valid = frame["method"] == "clarify" and {k: v for k, v in params.items() if k != "session_id"} == payload
        if not valid:
            raise ValueError("source request kind/payload mismatch; secrets are never projected")

    @staticmethod
    def _stable(frame, kind):
        if frame is not None and kind == "clarify":
            frame = {**frame, "params": {k: v for k, v in frame["params"].items() if k != "answers"}}
        return frame

    @staticmethod
    def _batch_result(req, response):
        from tools.clarify_batch import settle_batch
        if response.get("outcome") == "cancelled":
            return settle_batch(req.locked, ended="cancelled")
        answers = response.get("answers")
        if (not isinstance(answers, dict) or set(answers) != set(req.qids or [])
                or any(v is not None and (not isinstance(v, str) or len(v) > 16000) for v in answers.values())):
            return None
        # The original registry owns locks. Remote frames cannot replace an already locked answer.
        return settle_batch({**answers, **req.locked}, ended="submitted")

    def watch_tui(self, session_id, request_id, *, kind, payload, **options):
        from tui_gateway import server_requests as source
        current = lambda: self._stable(self._find(source.open_requests(session_id), request_id), kind)
        self._check_request(current(), kind, payload)
        resolve = lambda response: source.resolve_response({"jsonrpc": "2.0", "id": request_id, "result": response})
        if kind == "clarify":
            def resolve(response):
                with source._lock:
                    req = source._open.get(request_id)
                    if req is None or req.sid != session_id or req.method != "clarify":
                        return False
                    result = self._batch_result(req, response)
                    if result is None:
                        return False
                    source._open.pop(request_id)
                    req.result, req.answered = result, True
                req.event.set()
                return True
        return self._watch(request_id, kind=kind, payload=payload, current=current, resolve=resolve, **options)

    def watch_authority(self, events, session_id, request_id, *, kind, payload, **options):
        from gateway import session_server_requests as source
        current = lambda: self._stable(self._find(source.open_requests(events, session_id), request_id), kind)
        self._check_request(current(), kind, payload)
        # This runs inside the original owner, not on a remote actor's connection. Identity of
        # both the stream AND session must still match the source registration at resolution.
        resolve = lambda response: source.resolve(
            {"jsonrpc": "2.0", "id": request_id, "result": response},
            session_ok=lambda sid, stream: sid == session_id and stream is events)
        if kind == "clarify":
            def resolve(response):
                with source._lock:
                    pending = source._open.get(request_id)
                    if pending is None or pending.events is not events or pending.session_id != session_id:
                        return False
                    result = self._batch_result(pending, response)
                    if result is None:
                        return False
                    source._open.pop(request_id)
                    pending.result = result
                pending.event.set()
                return True
        return self._watch(request_id, kind=kind, payload=payload, current=current, resolve=resolve, **options)

    def watch_native_approval(self, session_key, request_id, **options):
        from tools import approval as source
        from agent.redact import redact_sensitive_text
        if not request_id:
            raise ValueError("exact approval queue id required")
        data = next((r for r in source.list_gateway_approvals(session_key) if r.get("request_id") == request_id), None)
        if data is None:
            return None
        payload = {"title": "Command approval", "command": redact_sensitive_text(str(data.get("command", "")), force=True),
                   "description": redact_sensitive_text(str(data.get("description", "")), force=True)}
        return self.watch_approval(session_key, request_id, payload=payload, allowed_choices=["once", "deny"], **options)

    def watch_native_clarify(self, request_id, **options):
        from tools import clarify_gateway as source
        entry = source.get_pending(request_id)
        if entry is None:
            return None
        payload = {"questions": [{"id": request_id, "question": entry.question,
                                  "choices": entry.choices, "multi_select": entry.multi_select}]}
        def current():
            live = source.get_pending(request_id)
            return payload if live is entry else None
        def commit(value):
            with source._lock:
                if source.get_pending(request_id) is not entry:
                    return False
                return source.resolve_gateway_clarify(request_id, value)
        def resolve(response):
            if response.get("outcome") == "cancelled":
                return commit(source.CANCELLED)
            answers = response.get("answers")
            if not isinstance(answers, dict) or set(answers) != {request_id}:
                return False
            value = answers[request_id]
            if value is None:
                value = source.SKIPPED
            elif not isinstance(value, str) or len(value) > 16000:
                return False
            return commit(value)
        return self._watch(request_id, kind="clarify", payload=payload, current=current, resolve=resolve, **options)

    def watch_approval(self, session_key, request_id, *, payload, allowed_choices, **options):
        from tools import approval as source
        def current():
            return next((r for r in source.list_gateway_approvals(session_key) if r.get("request_id") == request_id), None)
        resolve = lambda response: source.resolve_gateway_approval(session_key, response["choice"], request_id=request_id) == 1
        handle = self._watch(request_id, kind="approval", payload=payload, current=current, resolve=resolve,
                             allowed_choices=allowed_choices, deferred_ack=True, **options)
        # Additive observer: never steal Desktop's existing settle callback.
        if not source.observe_gateway_settle(session_key, request_id, lambda reason: self.settle(handle, reason)):
            self.settle(handle, "cancelled")
        return handle

    def settle(self, handle, reason, result=None):
        """Called only by the source owner after local answer, expiry, cancel or withdrawal."""
        status = {"resolved": "submitted", "timeout": "timed_out", "interrupted": "cancelled"}.get(reason, reason)
        if status not in TERMINAL:
            status = "cancelled"
        row = self.mailbox.get(handle)
        binding = self._bindings.get(handle)
        if row is None or binding is None:
            return False
        accepted_at = binding.accepted_at
        if status == "submitted" and self.mailbox._expired(row, accepted_at):
            status = "timed_out"
        accepted = self.mailbox._acknowledge(handle, owner_id=self.owner_id, status=status,
                                              result=result, accepted_at=accepted_at)
        if accepted:
            self._bindings.pop(handle, None)
        return accepted

    def drain(self):
        """Apply queued intents in the original source process. Sending is not settlement."""
        applied = 0
        with self._lock:
            for handle, binding in list(self._bindings.items()):
                row = self.mailbox.get(handle)
                if row["status"] in TERMINAL:
                    self._bindings.pop(handle, None)
                    continue
                if binding.awaiting_ack:
                    continue
                if self.mailbox._expired(row):
                    self.settle(handle, "timed_out")
                    continue
                if binding.digest != binding_digest(row) or binding.current() != binding.snapshot:
                    self.settle(handle, "cancelled")
                    continue
                if row["status"] != "submitting":
                    continue
                binding.awaiting_ack = binding.deferred_ack
                binding.accepted_at = time.time()
                if self.mailbox._expired(row, binding.accepted_at):
                    self.settle(handle, "timed_out")
                    continue
                response = row["response"]
                cancelled = row["kind"] != "approval" and response.get("action") == "cancel"
                if cancelled:
                    response = {"outcome": "cancelled", "surface": "telegram"}
                elif row["kind"] == "clarify" and response.get("action") == "submit":
                    # The rich renderer uses Ask-style answer objects; original clarify
                    # waiters require raw strings / JSON arrays / None, not that envelope.
                    import json
                    answers = response.get("payload", {}).get("answers", {})
                    converted = {}
                    valid = isinstance(answers, dict)
                    if valid:
                        for qid, answer in answers.items():
                            if not isinstance(answer, dict):
                                valid = False
                                break
                            value = None if answer.get("deferred") else answer.get("response")
                            if isinstance(value, list) and all(isinstance(v, str) for v in value):
                                value = json.dumps(value, ensure_ascii=False)
                            if value is not None and not isinstance(value, str):
                                valid = False
                                break
                            converted[qid] = value
                    if not valid:
                        self.settle(handle, "cancelled")
                        continue
                    response = {"answers": converted, "outcome": "submitted", "surface": "telegram"}
                elif row["kind"] == "ask" and response.get("action") == "submit":
                    response = {"payload": response["payload"], "surface": "telegram"}
                if binding.resolve(response):
                    applied += 1
                    if not binding.deferred_ack:
                        self.settle(handle, "cancelled" if cancelled else "submitted", response)
                else:
                    self.settle(handle, "cancelled")
        return applied
