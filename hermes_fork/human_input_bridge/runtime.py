"""Real source-owner lifecycle. No network listener, bot or model starts on import."""
from __future__ import annotations

import contextvars
import copy
import logging
import secrets
import threading
import time

from .mailbox import Mailbox
from .owner import OwnerBridge
from .routing import bind_ask_bridge

log = logging.getLogger(__name__)


class RuntimeOwner(OwnerBridge):
    def __init__(self, agent, settings, *, messaging=False):
        super().__init__(Mailbox(settings["mailbox_path"]), settings["telegram"])
        self.agent = agent
        self.delay_seconds = settings["delay_seconds"]
        self.messaging = messaging
        self._stop = threading.Event()
        self._side_active = set()
        self._side_forks = {}
        self._side_handles = {}
        self._context = contextvars.copy_context()
        self.events = None
        self.source_id = secrets.token_urlsafe(24)
        if messaging:
            from gateway.session_events import SessionEvents
            self.events = SessionEvents()
            self.events.fanout.attach(_MailboxTransport(self))
        self._pump = threading.Thread(target=self._context.run, args=(self._run,), daemon=True,
                                      name="human-input-owner")
        self._pump.start()

    def _run(self):
        while not self._stop.wait(0.02):
            try:
                self.drain()
                self._drain_sides()
            except Exception:
                log.exception("Human input owner pump failed")
                self._stop.set()

    def _drain_sides(self):
        for side_id, fork in list(self._side_forks.items()):
            if self._side_handles.get(side_id) not in self._bindings:
                fork.interrupt()
        for side in self.mailbox.side_requests(self.owner_id):
            key = side["side_id"]
            if key in self._side_active or side["handle"] not in self._bindings:
                continue
            self._side_active.add(key)
            self._side_handles[key] = side["handle"]
            context = contextvars.copy_context()
            threading.Thread(target=context.run, args=(self._answer_side, side),
                             name="human-input-side", daemon=True).start()

    def _answer_side(self, side):
        from agent.side_question import answer_side_question
        def on_fork(fork):
            if fork is None:
                self._side_forks.pop(side["side_id"], None)
            else:
                self._side_forks[side["side_id"]] = fork
                if self._closed:
                    fork.interrupt()
        try:
            history = copy.deepcopy(getattr(self.agent, "_session_messages", None) or getattr(self.agent, "conversation_history", []) or [])
            answer = answer_side_question(side["question"], history, parent_agent=self.agent,
                                          main_runtime={k: getattr(self.agent, k, None) for k in
                                                        ("model", "provider", "base_url", "api_key", "api_mode", "session_id")},
                                          on_fork=on_fork, should_stop=lambda: self._closed or side["handle"] not in self._bindings)
        except Exception:
            log.warning("Side question failed", exc_info=True)
            answer = "The side question could not be answered."
        if not self._closed:
            from agent.redact import redact_sensitive_text
            self.mailbox.acknowledge_side(side["handle"], side["side_id"], self.owner_id,
                                          redact_sensitive_text(answer or "", force=True)[:16000])

    def request(self, kind, payload, timeout):
        if kind != "fork-ask/questions" or self.events is None:
            return None
        from gateway.session_server_requests import ask_viewer_result
        expiry = time.time() + timeout if timeout is not None else None
        try:
            return ask_viewer_result(self.events, self.source_id, "plugin.request",
                                     {"kind": kind, "payload": payload, "fork_input_expires_at": expiry}, timeout=timeout,
                                     cancelled=lambda: self._closed or bool(getattr(self.agent, "_interrupt_requested", False)))
        finally:
            self.drain()

    def close(self):
        self._stop.set()
        if threading.current_thread() is not self._pump:
            self._pump.join()
        super().close()
        for fork in list(self._side_forks.values()):
            fork.interrupt()
        if self.events is not None:
            from gateway.session_server_requests import withdraw
            withdraw(self.events, self.source_id, "plugin.request")
            self.events.release()


class _MailboxTransport:
    """A source-process viewer backed by the mailbox, not a second request registry."""
    def __init__(self, owner):
        self.owner = owner

    def write(self, frame):
        if frame.get("method") == "plugin.request":
            owner = self.owner
            owner.watch_authority(owner.events, owner.source_id, frame["id"], kind="ask",
                                  payload=frame["params"]["payload"], delay_seconds=0,
                                  expires_at=frame["params"]["fork_input_expires_at"])
        return True

    def close(self):
        return None


def bind_agent(agent, config=None, *, messaging=False):
    """Call in the owning profile scope; config is captured once per turn, never each tick."""
    from hermes_constants import get_hermes_home
    from hermes_fork.config import human_input_bridge
    release_agent(agent)
    if config is None:
        from hermes_cli.config import load_config_readonly
        config = load_config_readonly()
    settings = human_input_bridge(config, home=get_hermes_home())
    if settings is None:
        return None
    owner = RuntimeOwner(agent, settings, messaging=messaging)
    bind_ask_bridge(agent, owner)
    return owner


def release_agent(agent):
    owner = getattr(agent, "_fork_human_input_owner", None)
    bind_ask_bridge(agent, None)
    if owner is not None:
        owner.close()
