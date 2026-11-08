"""Telegram title projection ownership and SQLite title evolution contracts."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def telegram_accounting(monkeypatch):
    from contextvars import ContextVar
    import agent.aux_accounting as accounting

    context = (SimpleNamespace(get_session=lambda sid: {'source': 'telegram'}), 's1')
    monkeypatch.setattr(accounting, '_accounting', ContextVar('test_title_accounting', default=context))


def test_manual_topic_protection_survives_reconstruction(tmp_path):
    from hermes_fork.telegram_titles import TopicLedger

    key = ('printlab', '42', '100', '7')
    ledger = TopicLedger(tmp_path)
    ledger.created(key, 'Opening request', implicit=True)
    assert ledger.get(key)['managed'] is True
    ledger.edited(key, 'My chosen name', human=True)
    assert TopicLedger(tmp_path).get(key)['managed'] is False


def make_projection(tmp_path):
    from hermes_fork.telegram_titles import TopicLedger, TitleProjector
    from gateway.config import Platform
    from gateway.session import SessionSource
    from hermes_state import SessionDB

    db = SessionDB(tmp_path / 'state.db')
    db.create_session('s1', source='telegram')
    db.enable_telegram_topic_mode(chat_id='100', user_id='5', profile_name='printlab')
    db.bind_telegram_topic(chat_id='100', thread_id='7', user_id='5', session_key='agent:printlab:telegram:dm:100:7', session_id='s1', profile_name='printlab')
    db.set_auto_title('s1', 'Printer connection', source='llm')
    source = SessionSource(platform=Platform.TELEGRAM, user_id='5', chat_id='100',
                           chat_type='dm', thread_id='7', profile='printlab')
    bot = SimpleNamespace(id=42, edit_forum_topic=AsyncMock(return_value=True))
    adapter = SimpleNamespace(_bot=bot, config=SimpleNamespace(extra={}))
    runner = SimpleNamespace(_delivery_adapter_for=lambda source: adapter,
                             _telegram_topic_profile_name=lambda source: 'printlab',
                             _telegram_topic_auto_rename_disabled=lambda source: False,
                             _sync_session_db=lambda: db)
    ledger = TopicLedger(tmp_path)
    key = ('printlab', '42', '100', '7')
    ledger.created(key, 'Opening request', implicit=True)
    return TitleProjector(runner, ledger), db, source, bot, ledger, key


@pytest.mark.asyncio
async def test_real_sqlite_title_cas_projects_once(tmp_path):
    projector, db, source, bot, ledger, key = make_projection(tmp_path)
    assert await projector.project(source, 's1')
    assert not await projector.project(source, 's1')
    assert bot.edit_forum_topic.await_count == 1
    db._execute_write(lambda conn: conn.execute(
        "UPDATE sessions SET title=? WHERE id=? AND title_source='llm'",
        ('Printer repair', 's1')))
    assert await projector.project(source, 's1')
    assert bot.edit_forum_topic.await_args.kwargs['name'] == 'Printer repair'
    assert ledger.get(key)['last_title'] == 'Printer repair'
    db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('guard', ['manual', 'fixed', 'missing_binding', 'rebound', 'user_title', 'unknown', 'legacy', 'derived', 'detached'])
async def test_protected_or_stale_topic_never_renames(tmp_path, guard):
    projector, db, source, bot, ledger, key = make_projection(tmp_path)
    if guard == 'manual':
        ledger.edited(key, 'Manual name', human=True)
    elif guard == 'fixed':
        projector.runner._delivery_adapter_for(source).config.extra['dm_topics'] = [
            {'chat_id': 100, 'topics': [{'name': 'Chat', 'thread_id': 7}]}]
    elif guard == 'missing_binding':
        db.delete_telegram_topic_binding(chat_id='100', thread_id='7', profile_name='printlab')
    elif guard == 'rebound':
        db.create_session('s2', source='telegram')
        db.bind_telegram_topic(chat_id='100', thread_id='7', user_id='5', session_key='agent:printlab:telegram:dm:100:7', session_id='s2', profile_name='printlab')
    elif guard == 'user_title':
        db.set_session_title('s1', 'My chosen name')
    elif guard == 'unknown':
        projector.ledger = type(ledger)(tmp_path / 'other')
    elif guard in ('legacy', 'derived'):
        db._execute_write(lambda conn: conn.execute('UPDATE sessions SET title_source=? WHERE id=?', (None if guard == 'legacy' else 'derived', 's1')))
    elif guard == 'detached':
        from gateway.session_identity import replace_source
        source = replace_source(source, detached_route='agent:printlab:detached:s1')
    assert not await projector.project(source, 's1')
    bot.edit_forum_topic.assert_not_awaited()
    db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('receipt', [False, None, 1, {'ok': True}])
async def test_false_api_receipt_does_not_advance_projection(tmp_path, receipt):
    projector, db, source, bot, ledger, key = make_projection(tmp_path)
    bot.edit_forum_topic.return_value = receipt
    assert not await projector.project(source, 's1')
    assert ledger.get(key)['last_title'] is None
    bot.edit_forum_topic.return_value = True
    assert await projector.project(source, 's1')
    db.close()


def test_short_name_policy_rejects_sentences_not_truncates():
    from hermes_fork.telegram_titles import short_name

    assert short_name('  Printer   connection  ') == 'Printer connection'
    assert short_name('A whole sentence about fixing the printer') is None
    assert short_name('Proceed with option 1') is None
    assert short_name('```') is None


@pytest.mark.asyncio
async def test_existing_gateway_rename_delegates_to_guard(tmp_path, monkeypatch):
    import hermes_fork.telegram_titles as titles
    from gateway.run_topics import GatewayTopicThreadsMixin

    projector, db, source, bot, _ledger, _key = make_projection(tmp_path)
    shim = AsyncMock(return_value=True)
    monkeypatch.setattr(titles, 'rename_topic_title', shim)
    await GatewayTopicThreadsMixin._rename_telegram_topic_for_session_title(projector.runner, source, 's1', 'Printer repair')
    shim.assert_awaited_once_with(projector.runner, source, 's1', 'Printer repair')
    bot.edit_forum_topic.assert_not_awaited()
    db.close()


@pytest.mark.asyncio
async def test_watcher_coalesces_and_later_commit_projects(tmp_path):
    projector, db, source, bot, _ledger, _key = make_projection(tmp_path)
    original = projector.project
    calls = 0

    async def commit_then_project(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            db._execute_write(lambda conn: conn.execute('UPDATE sessions SET title=? WHERE id=?', ('Printer repair', 's1')))
        return await original(*args, **kwargs)

    projector.project = commit_then_project
    old = projector.arm(source, 's1', current=lambda: True, delays=(0, 0))
    task = projector.arm(source, 's1', current=lambda: True, delays=(0, 0))
    await asyncio.gather(old, task, return_exceptions=True)
    assert old.cancelled()
    assert bot.edit_forum_topic.await_count == 2
    assert bot.edit_forum_topic.await_args.kwargs['name'] == 'Printer repair'
    assert not projector.watchers
    db.close()


@pytest.mark.asyncio
async def test_stale_generation_and_api_exception_do_not_advance(tmp_path):
    projector, db, source, bot, ledger, key = make_projection(tmp_path)
    assert not await projector.project(source, 's1', current=lambda: False)
    bot.edit_forum_topic.assert_not_awaited()
    bot.edit_forum_topic.side_effect = RuntimeError('offline')
    assert not await projector.project(source, 's1')
    assert ledger.get(key)['last_title'] is None
    db.close()


@pytest.mark.asyncio
async def test_non_telegram_sources_do_not_load_optional_config(monkeypatch):
    import hermes_fork.telegram_titles as titles

    monkeypatch.setattr(titles, '_settings', lambda home: pytest.fail('unrelated source read configuration'))
    source = SimpleNamespace(platform=SimpleNamespace(value='discord'), chat_type='dm', thread_id='7')
    assert not await titles.rename_topic_title(SimpleNamespace(), source, 's1', 'Printer repair')
    await titles.observe_topic_title(SimpleNamespace(), source, 'key', 's1', 'key', 1)


def test_initial_title_prompt_uses_opt_in_short_policy(monkeypatch, telegram_accounting):
    import hermes_fork.telegram_titles as titles

    monkeypatch.setattr(titles, '_settings', lambda home: {'enabled': True})
    original = 'Title instructions: 3 to 7 words. Return JSON.'
    rewritten = titles.title_prompt(original)
    assert '2 to 4 words' in rewritten
    assert '3 to 7 words' not in rewritten
    assert 'Return JSON.' in rewritten
    monkeypatch.setattr(titles, '_settings', lambda home: None)
    assert titles.title_prompt(original) == original


def test_real_initial_title_call_uses_short_prompt(monkeypatch, telegram_accounting):
    import hermes_fork.telegram_titles as titles
    import agent.title_generator as generator

    monkeypatch.setattr(titles, '_settings', lambda home: {'enabled': True})
    monkeypatch.setattr(generator, '_auto_title_enabled', lambda: True)
    monkeypatch.setattr(generator, '_title_language', lambda: '')
    requests = []

    def completion(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"title":"Printer connection"}'))])

    monkeypatch.setattr(generator, 'call_llm', completion)
    assert generator.generate_title('Investigate printer connection') == 'Printer connection'
    assert len(requests) == 1
    assert '2 to 4 words' in requests[0]['messages'][0]['content']
    assert '3 to 7 words' not in requests[0]['messages'][0]['content']


@pytest.mark.asyncio
async def test_native_observer_auth_manual_edits_and_captured_profile(tmp_path, monkeypatch):
    import hermes_fork.telegram_titles as titles
    import hermes_constants
    import hermes_cli.profiles

    monkeypatch.setattr(titles, '_settings', lambda home: {'enabled': True})
    monkeypatch.setattr(hermes_constants, 'get_hermes_home', lambda: tmp_path)
    monkeypatch.setattr(hermes_cli.profiles, 'get_active_profile_name', lambda: 'printlab')
    handlers = []
    app = SimpleNamespace(bot_data={}, add_handler=lambda handler, group: handlers.append((handler, group)))
    adapter = SimpleNamespace(_bot=SimpleNamespace(id=42), _is_user_authorized_from_message=lambda m: m.from_user.id == 5)
    titles.register_topic_title_observers(adapter, app)
    assert len(handlers) == 1 and handlers[0][1] == 98
    callback = handlers[0][0].callback
    ledger = titles.TopicLedger(tmp_path)
    key = ('printlab', '42', '100', '7')

    def update(uid, *, bot=False, created=None, edited=None):
        return SimpleNamespace(effective_message=SimpleNamespace(
            chat=SimpleNamespace(type='private', id=100), message_thread_id=7,
            from_user=SimpleNamespace(id=uid, is_bot=bot), forum_topic_created=created, forum_topic_edited=edited))

    creation = SimpleNamespace(name='Opening request', api_kwargs={'is_name_implicit': True})
    await callback(update(6, created=creation), None)
    assert ledger.get(key) is None
    monkeypatch.setattr(hermes_cli.profiles, 'get_active_profile_name', lambda: 'homelab')
    await callback(update(5, created=creation), None)
    assert ledger.get(key)['managed'] is True
    ledger.expect(key, 'Printer repair')
    await callback(update(42, bot=True, edited=SimpleNamespace(name='Printer repair')), None)
    assert ledger.get(key)['managed'] is True
    await callback(update(5, edited=SimpleNamespace(name='My printer')), None)
    assert titles.TopicLedger(tmp_path).get(key)['managed'] is False
    assert ledger.get(('homelab', '42', '100', '7')) is None
    assert len(handlers) == 1


@pytest.mark.asyncio
async def test_real_slotted_application_registration_and_rebuild(tmp_path, monkeypatch):
    import hermes_fork.telegram_titles as titles
    import hermes_constants
    from telegram.ext import Application
    from plugins.platforms.telegram.update_admission import TelegramApplication

    monkeypatch.setattr(titles, '_settings', lambda home: {'enabled': True})
    monkeypatch.setattr(hermes_constants, 'get_hermes_home', lambda: tmp_path)
    adapter = SimpleNamespace(_update_admission=None)
    app = Application.builder().token('123:offline-test').application_class(TelegramApplication, kwargs={'adapter': adapter}).build()
    titles.register_topic_title_observers(adapter, app)
    titles.register_topic_title_observers(adapter, app)
    assert len(app.handlers[98]) == 1
    rebuilt = Application.builder().token('123:offline-test').application_class(TelegramApplication, kwargs={'adapter': adapter}).build()
    titles.register_topic_title_observers(adapter, rebuilt)
    assert len(rebuilt.handlers[98]) == 1


@pytest.mark.asyncio
async def test_transport_profile_and_runtime_binding_are_distinct(tmp_path):
    projector, db, source, bot, ledger, _key = make_projection(tmp_path)
    adapter = projector.runner._delivery_adapter_for(source)
    adapter._short_title_profile = 'default'
    ledger.edited(_key, 'Wrong runtime ownership', human=True)
    ledger.created(('default', '42', '100', '7'), 'Opening request', implicit=True)
    assert await projector.project(source, 's1')
    bot.id = 43
    assert not await projector.project(source, 's1')
    assert bot.edit_forum_topic.await_count == 1
    db.close()


@pytest.mark.asyncio
async def test_observed_manual_edit_during_reservation_cancels_egress(tmp_path, monkeypatch):
    projector, db, source, bot, ledger, key = make_projection(tmp_path)
    original = ledger.expect

    def interleave(k, title):
        original(k, title)
        ledger.edited(k, 'Human title', human=True)

    monkeypatch.setattr(ledger, 'expect', interleave)
    assert not await projector.project(source, 's1')
    bot.edit_forum_topic.assert_not_awaited()
    assert ledger.get(key)['managed'] is False
    db.close()


@pytest.mark.asyncio
async def test_authoritative_live_fixed_topic_lookup_prevents_rename(tmp_path):
    projector, db, source, bot, _ledger, _key = make_projection(tmp_path)

    class LiveAdapter(SimpleNamespace):
        def _get_dm_topic_info(self, chat_id, thread_id):
            return {'name': 'Chat'}

    old = projector.runner._delivery_adapter_for(source)
    live = LiveAdapter(_bot=bot, config=old.config)
    projector.runner._delivery_adapter_for = lambda source: live
    assert not await projector.project(source, 's1')
    bot.edit_forum_topic.assert_not_awaited()
    db.close()


def test_non_telegram_initial_prompt_unchanged_when_enabled(monkeypatch):
    import hermes_fork.telegram_titles as titles
    from agent.aux_accounting import set_accounting_context, reset_accounting_context

    monkeypatch.setattr(titles, '_settings', lambda home: {'enabled': True})
    original = 'Title instructions: 3 to 7 words. Return JSON.'
    for source in ['desktop', 'discord', 'cli']:
        token = set_accounting_context(SimpleNamespace(get_session=lambda sid: {'source': source}), 's1')
        try:
            assert titles.title_prompt(original) == original
        finally:
            reset_accounting_context(token)


@pytest.mark.asyncio
async def test_post_window_title_change_reconciles_on_next_turn(tmp_path, monkeypatch):
    import hermes_fork.telegram_titles as titles

    projector, db, source, bot, _ledger, _key = make_projection(tmp_path)
    projector.runner._is_session_run_current = lambda key, generation: True
    monkeypatch.setattr(titles, '_runtime', lambda runner, source: projector)
    original_arm = projector.arm
    tasks = []

    def finite_arm(*args, **kwargs):
        task = original_arm(*args, **kwargs, delays=(0,))
        tasks.append(task)
        return task

    monkeypatch.setattr(projector, 'arm', finite_arm)
    await titles.observe_topic_title(projector.runner, source, 'key', 's1', 'key', 1)
    await tasks[-1]
    assert not projector.watchers
    db._execute_write(lambda conn: conn.execute('UPDATE sessions SET title=? WHERE id=?', ('Printer repair', 's1')))
    await titles.observe_topic_title(projector.runner, source, 'key', 's1', 'key', 2)
    await tasks[-1]
    assert bot.edit_forum_topic.await_count == 2
    assert bot.edit_forum_topic.await_args.kwargs['name'] == 'Printer repair'
    db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('fixed', [False, True])
async def test_explicit_title_command_preserves_manual_intent(tmp_path, monkeypatch, fixed):
    import hermes_fork.telegram_titles as titles

    projector, db, source, bot, ledger, key = make_projection(tmp_path)
    title = 'My personally chosen printer setup title'
    db.set_session_title('s1', title)
    if fixed:
        projector.runner._delivery_adapter_for(source).config.extra['dm_topics'] = [
            {'chat_id': 100, 'topics': [{'name': 'Chat', 'thread_id': 7}]}]
    monkeypatch.setattr(titles, '_runtime', lambda runner, source: projector)
    await titles.manual_topic_title(projector.runner, source, 's1', title)
    if fixed:
        bot.edit_forum_topic.assert_not_awaited()
    else:
        assert bot.edit_forum_topic.await_args.kwargs['name'] == title
        assert ledger.get(key)['managed'] is False
    assert not await projector.project(source, 's1')
    db.close()

