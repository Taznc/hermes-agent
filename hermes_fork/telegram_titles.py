"""Opt-in, profile-local ownership for short Telegram topic-title projections.

This compatibility shim does not generate titles or change session transcripts.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

_RUNTIME_LOCK = threading.Lock()
_EPOCH_LOCK = threading.Lock()
_MANUAL_EPOCHS = {}


class TopicLedger:
    """Persist only exact topic ownership and acknowledged projection facts."""

    def __init__(self, home):
        directory = Path(home) / 'telegram-titles'
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = directory / 'ownership.sqlite3'
        with self.connect() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS topics (
                profile TEXT, bot_id TEXT, chat_id TEXT, thread_id TEXT,
                managed INTEGER NOT NULL, last_title TEXT, expected_title TEXT,
                PRIMARY KEY(profile, bot_id, chat_id, thread_id))''')
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=2)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def get(self, key):
        with self.connect() as conn:
            row = conn.execute('SELECT * FROM topics WHERE profile=? AND bot_id=? AND chat_id=? AND thread_id=?', key).fetchone()
        return {**dict(row), 'managed': bool(row['managed'])} if row else None

    def created(self, key, name, *, implicit):
        # A duplicate creation notification cannot erase persisted human ownership.
        with self.connect() as conn:
            conn.execute('INSERT OR IGNORE INTO topics VALUES (?, ?, ?, ?, ?, NULL, ?)', (*key, int(implicit is True), name))

    def epoch(self, key):
        with _EPOCH_LOCK:
            return _MANUAL_EPOCHS.get((str(self.path), key), 0)

    def revoke(self, key):
        with _EPOCH_LOCK:
            identity = (str(self.path), key)
            _MANUAL_EPOCHS[identity] = _MANUAL_EPOCHS.get(identity, 0) + 1

    def edited(self, key, name, *, human):
        row = self.get(key)
        if row and not human and name in (row['expected_title'], row['last_title']):
            return
        self.revoke(key)
        with self.connect() as conn:
            conn.execute('''INSERT INTO topics VALUES (?, ?, ?, ?, 0, NULL, NULL)
                ON CONFLICT(profile, bot_id, chat_id, thread_id) DO UPDATE SET managed=0, expected_title=NULL''', key)

    def expect(self, key, title):
        with self.connect() as conn:
            conn.execute('UPDATE topics SET expected_title=? WHERE profile=? AND bot_id=? AND chat_id=? AND thread_id=?', (title, *key))

    def projected(self, key, title):
        with self.connect() as conn:
            conn.execute('UPDATE topics SET last_title=?, expected_title=? WHERE profile=? AND bot_id=? AND chat_id=? AND thread_id=? AND managed=1', (title, title, *key))


def short_name(title):
    """Reject sentence/acknowledgement output rather than storing a chopped sentence."""
    title = ' '.join(str(title or '').split())
    if not title or len(title) > 36 or len(title.split()) > 4 or not any(c.isalnum() for c in title):
        return None
    if re.match(r'(?i)^(proceed\b|yes\b|okay\b|ok\b|continue\b|sounds good\b)', title):
        return None
    return title


def fixed_topic(adapter, home, key):
    get_info = getattr(type(adapter), '_get_dm_topic_info', None)
    if not callable(get_info):
        return False
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override

    token = set_hermes_home_override(adapter.__dict__.get('_short_title_home') or home)
    try:
        return isinstance(get_info(adapter, key[2], key[3]), dict)
    finally:
        reset_hermes_home_override(token)


class TitleProjector:
    """One AI writer, serialized projection; all SQLite work runs off the loop."""

    def __init__(self, runner, ledger):
        self.runner = runner
        self.ledger = ledger
        self.locks = {}
        self.watchers = {}

    def route(self, source):
        from gateway.session_detached import detached_route_of

        platform = getattr(getattr(source, 'platform', None), 'value', None)
        if platform != 'telegram' or source.chat_type != 'dm' or str(source.thread_id or '') in ('', '0', '1'):
            return None
        if detached_route_of(source) is not None or self.runner._telegram_topic_auto_rename_disabled(source):
            return None
        adapter = self.runner._delivery_adapter_for(source)
        bot = getattr(adapter, '_bot', None)
        if bot is None or not getattr(bot, 'id', None):
            return None
        extra = getattr(getattr(adapter, 'config', None), 'extra', {}) or {}
        for entry in extra.get('dm_topics', []):
            if str(entry.get('chat_id')) == str(source.chat_id):
                if any(str(t.get('thread_id')) == str(source.thread_id) for t in entry.get('topics', [])):
                    return None
        transport_profile = adapter.__dict__.get('_short_title_profile') or self.runner._telegram_topic_profile_name(source)
        key = (transport_profile, str(bot.id), str(source.chat_id), str(source.thread_id))
        return key, bot

    def candidate(self, source, session_id, key):
        row = self.ledger.get(key)
        if not row or row['managed'] is not True:
            return None
        db = self.runner._sync_session_db()
        if db is None:
            return None
        adapter = self.runner._delivery_adapter_for(source)
        if fixed_topic(adapter, self.ledger.path.parent.parent, key):
            return None
        binding = db.get_telegram_topic_binding(chat_id=key[2], thread_id=key[3], profile_name=self.runner._telegram_topic_profile_name(source))
        if not binding or binding.get('session_id') != session_id:
            return None
        session = db.get_session(session_id)
        if not session or session.get('title_source') != 'llm':
            return None
        title = short_name(session.get('title'))
        return title if title and title != row['last_title'] else None

    async def project(self, source, session_id, *, current=lambda: True):
        route = self.route(source)
        if route is None:
            return False
        key, bot = route
        async with self.locks.setdefault(key, asyncio.Lock()):
            epoch = self.ledger.epoch(key)
            if not current():
                return False
            title = await asyncio.to_thread(self.candidate, source, session_id, key)
            if title is None:
                return False
            # Recheck after the off-loop read; an old turn may have lost its route.
            if not current() or self.route(source) != route:
                return False
            latest = await asyncio.to_thread(self.candidate, source, session_id, key)
            if latest != title or not current():
                return False
            await asyncio.to_thread(self.ledger.expect, key, title)
            latest = await asyncio.to_thread(self.candidate, source, session_id, key)
            if latest != title or not current() or self.ledger.epoch(key) != epoch or self.route(source) != route:
                return False
            try:
                acknowledged = await bot.edit_forum_topic(chat_id=int(key[2]), message_thread_id=int(key[3]), name=title)
            except Exception:
                logging.getLogger(__name__).debug('Topic title projection failed')
                acknowledged = False
            if acknowledged is not True:
                await asyncio.to_thread(self.ledger.expect, key, None)
                return False
            await asyncio.to_thread(self.ledger.projected, key, title)
            return True

    def arm(self, source, session_id, *, current, delays=(0, 1, 2, 5, 10, 15, 30)):
        route = self.route(source)
        if route is None:
            return None
        key = route[0]
        previous = self.watchers.get(key)
        if previous is not None:
            previous.cancel()

        async def watch():
            try:
                for delay in delays:
                    if delay:
                        await asyncio.sleep(delay)
                    if not current():
                        return
                    await self.project(source, session_id, current=current)
            except asyncio.CancelledError:
                pass
            except Exception:
                logging.getLogger(__name__).debug('Topic title observation failed', exc_info=True)
            finally:
                if self.watchers.get(key) is asyncio.current_task():
                    self.watchers.pop(key, None)

        task = asyncio.create_task(watch())
        self.watchers[key] = task
        return task


def _settings(home):
    from hermes_cli.config import read_user_config_raw

    try:
        config = read_user_config_raw(Path(home) / 'config.yaml')
        fork = config.get('fork') or {}
        settings = fork.get('telegram_titles') if isinstance(fork, dict) else None
        return settings if isinstance(settings, dict) and settings.get('enabled') is True else None
    except (OSError, TypeError, ValueError, AttributeError):
        return None


def title_prompt(prompt):
    """Apply the operator preference to the existing initial AI title request."""
    from hermes_constants import get_hermes_home

    # auto_title_session already publishes its exact accounting/session context.
    # Read it without mutating the owner's ContextVar or changing public signatures.
    from agent.aux_accounting import _accounting

    context = _accounting.get()
    if context is None or not _settings(get_hermes_home()):
        return prompt
    try:
        session = context[0].get_session(context[1])
        if not isinstance(session, dict) or session.get('source') != 'telegram':
            return prompt
    except Exception:
        return prompt
    return prompt.replace('3 to 7 words', '2 to 4 words') + (
        '\nOperator preference: short meaningful subject names, generally 2 to 4 words, '
        'at most 36 characters. Preserve essential identifiers. Do not use an '
        'acknowledgement such as Proceed, Yes or Continue as the subject.\n')


def _eligible_source(source):
    return (getattr(getattr(source, 'platform', None), 'value', None) == 'telegram'
            and getattr(source, 'chat_type', None) == 'dm'
            and str(getattr(source, 'thread_id', None) or '') not in ('', '0', '1'))


def _runtime(runner, source):
    from hermes_constants import get_hermes_home

    adapter = runner._delivery_adapter_for(source)
    if adapter is None:
        return None
    # Registration pins the transport home/settings. Older adapters resolve it
    # once here; never reparse a large YAML file on every watcher tick.
    with _RUNTIME_LOCK:
        settings = adapter.__dict__.get('_short_title_settings', 'unresolved')
        home = adapter.__dict__.get('_short_title_home')
        if settings == 'unresolved':
            home = Path(runner._authorization_home_for_source(source) or get_hermes_home()).resolve()
            settings = _settings(home)
            adapter._short_title_home, adapter._short_title_settings = home, settings
        if not settings:
            return None
        runtimes = runner.__dict__.setdefault('_fork_topic_projectors', {})
        if home not in runtimes:
            runtimes[home] = TitleProjector(runner, TopicLedger(home))
        return runtimes[home]


async def rename_topic_title(runner, source, session_id, title):
    """True means the opt-in shim owns this attempt, including safe no-ops."""
    if not _eligible_source(source):
        return False
    try:
        projector = await asyncio.to_thread(_runtime, runner, source)
        if projector is None:
            return False
        await projector.project(source, session_id)
        return True
    except Exception:
        # When enabled, an uncertain ownership check must not fall through to
        # the less restrictive legacy rename implementation.
        logging.getLogger(__name__).debug('Short topic title guard failed', exc_info=True)
        return True


async def manual_topic_title(runner, source, session_id, title):
    """Explicit /title remains authoritative; automatic observers cannot use this path."""
    if not _eligible_source(source):
        await asyncio.to_thread(runner._schedule_telegram_topic_title_rename, source, session_id, title)
        return
    try:
        projector = await asyncio.to_thread(_runtime, runner, source)
        if projector is None:
            await asyncio.to_thread(runner._schedule_telegram_topic_title_rename, source, session_id, title)
            return
        route = projector.route(source)
        if route is None:
            return
        key, bot = route
        projector.ledger.revoke(key)
        async with projector.locks.setdefault(key, asyncio.Lock()):
            def authorized():
                db = runner._sync_session_db()
                binding = db.get_telegram_topic_binding(chat_id=key[2], thread_id=key[3], profile_name=runner._telegram_topic_profile_name(source))
                row = db.get_session(session_id)
                adapter = runner._delivery_adapter_for(source)
                if fixed_topic(adapter, projector.ledger.path.parent.parent, key):
                    return False
                return (isinstance(binding, dict) and binding.get('session_id') == session_id
                        and isinstance(row, dict) and row.get('title_source') == 'user' and row.get('title') == title)

            if not await asyncio.to_thread(authorized) or projector.route(source) != route:
                return
            await asyncio.to_thread(projector.ledger.edited, key, title, human=True)
            if projector.route(source) != route:
                return
            await bot.edit_forum_topic(chat_id=int(key[2]), message_thread_id=int(key[3]), name=title)
    except Exception:
        logging.getLogger(__name__).debug('Explicit topic title projection unavailable')


async def observe_topic_title(runner, source, session_key, session_id, quick_key, generation):
    """Arm a finite 63-second window; later commits reconcile on the next turn."""
    if not _eligible_source(source):
        return
    try:
        projector = await asyncio.to_thread(_runtime, runner, source)
        if projector is None:
            return
        import copy

        source = copy.copy(source)
        projector.arm(source, session_id, current=lambda: runner._is_session_run_current(quick_key, generation))
    except Exception:
        logging.getLogger(__name__).debug('Short topic title observation unavailable', exc_info=True)


def register_topic_title_observers(adapter, app):
    """Observe authorized topic service updates alongside native PTB handlers."""
    from hermes_constants import get_hermes_home

    home = Path(get_hermes_home()).resolve()
    settings = _settings(home)
    adapter._short_title_home, adapter._short_title_settings = home, settings
    if not settings or app.bot_data.get('_fork_title_observer', False):
        return
    from telegram.ext import MessageHandler, filters

    created_filter = getattr(filters.StatusUpdate, 'FORUM_TOPIC_CREATED', None)
    edited_filter = getattr(filters.StatusUpdate, 'FORUM_TOPIC_EDITED', None)
    if created_filter is None or edited_filter is None:
        return
    from hermes_cli.profiles import get_active_profile_name

    adapter._short_title_home = home
    profile = get_active_profile_name() or 'default'
    adapter._short_title_profile = profile
    ledger = TopicLedger(home)

    async def observe(update, context):
        message = getattr(update, 'effective_message', None)
        if message is None or getattr(getattr(message, 'chat', None), 'type', None) != 'private':
            return
        sender = getattr(message, 'from_user', None)
        bot = getattr(adapter, '_bot', None)
        if sender is None or bot is None:
            return
        own_bot = str(sender.id) == str(bot.id)
        if not own_bot and (getattr(sender, 'is_bot', False) or not adapter._is_user_authorized_from_message(message)):
            return
        thread = getattr(message, 'message_thread_id', None)
        if thread is None:
            return
        # Capture the transport profile at registration, never from a later
        # callback's ambient profile scope.
        key = (profile, str(bot.id), str(message.chat.id), str(thread))
        created = getattr(message, 'forum_topic_created', None)
        edited = getattr(message, 'forum_topic_edited', None)
        if created is not None:
            implicit = getattr(created, 'is_name_implicit', None)
            if implicit is None:
                implicit = (getattr(created, 'api_kwargs', None) or {}).get('is_name_implicit')
            await asyncio.to_thread(ledger.created, key, created.name, implicit=implicit is True)
        elif edited is not None and getattr(edited, 'name', None) is not None:
            if not own_bot:
                ledger.revoke(key)  # invalidate synchronously before any worker await
            await asyncio.to_thread(ledger.edited, key, edited.name, human=not own_bot)

    app.add_handler(MessageHandler(created_filter | edited_filter, observe), group=98)
    app.bot_data['_fork_title_observer'] = True

