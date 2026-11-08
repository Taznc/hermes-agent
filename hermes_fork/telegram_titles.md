# Short Telegram title compatibility shim

Status: opt-in; no second AI title writer or bot poller.

## Configuration

```yaml
fork:
  telegram_titles:
    enabled: true
plugins:
  entries:
    hermes-auto-titler:
      settings:
        first_title_mode: builtin
        title_style: concise
        custom_instructions: >-
          Prefer meaningful subject names of 2–4 words, at most 36 characters.
          Preserve essential identifiers. Never title a conversation after an
          acknowledgement. Do not rename merely to shorten a still-correct title.
        max_display_width: 36
        max_title_length: 36
```

Hermes generates the first AI title. The initial-prompt shim checks the exact
session source from the already-published auxiliary accounting context; CLI,
Desktop and Discord prompts remain unchanged. When installed/enabled, the existing
hermes-auto-titler maintains subsequent titles. This shim changes the initial
prompt preference and projects eligible saved titles; it makes no AI request.
Without the existing maintenance plugin, initial titles still work, but there
is no new continuous AI title generation supplied by this shim.

## Ownership and lifecycle

- Observe authorized private Telegram topic creation/edit service messages in a
  separate native PTB handler group. No messages are consumed as agent input.
- Only an explicitly implicit-name creation grants machine ownership. Missing
  provenance and existing topics fail closed. A manually chosen topic name is
  not permission to replace it automatically. Human edits revoke ownership.
- Keep state in `<transport-home>/telegram-titles/ownership.sqlite3`; mode 0600
  inside a private directory. It contains identifiers and title metadata, not
  conversation text or tokens. No core SessionDB migration is added.
- Partition by profile, bot identity, private chat and thread. Require exact
  present session binding and `llm` title provenance. Skip configured topics,
  detached sources and disabled automatic renaming. User and unknown legacy
  provenance never triggers automatic projection.
- Initial-title callbacks and per-turn observation share the same serialized
  projector. A confirmed Bot API `True` receipt advances the projected title.
  False/no bot/exception leaves it retryable. The SDK facade's permissive
  success wrapper is deliberately not used as proof.
- Reject sentence-sized/acknowledgement titles; do not cut a long title in the
  middle to meet the word budget. One essential identifier may be shorter than
  two words. Short-name preference is normally 2–4 words and at most 36 chars.
- Observe during a finite 63-second window per eligible turn, coalescing old
  watchers. A later asynchronous title commit reconciles on the next turn.
  This is not a promise of immediate arbitrary idle-time synchronization.
- Generation and binding changes invalidate old work. Telegram offers no
  conditional-name CAS, so an unseen external manual edit racing an in-flight
  Bot API call cannot be made atomic by this shim.

## Removable seams

Named T2 anchors, one call site each, no signatures changed:
`agent/title_generator.py`, `gateway/run_topics.py`, `gateway/run_turn.py`,
`gateway/slash_commands_session.py`, `plugins/platforms/telegram/adapter.py`.
Explicit `/title` requests have a separate user-authoritative path: they may
rename an ordinary topic and revoke future automatic ownership, but cannot
rename fixed configured topics. All behavior is fork-owned in
`hermes_fork/telegram_titles.py`; tests in `tests/hermes_fork/test_telegram_titles.py`.

Upstream has overlapping compact-topic/manual-title proposals; remove this
compatibility shim when the shipped upstream contract covers the behavior.
No upstream issue/PR publication is part of this work.

## Verification

Run `scripts/run_tests.sh` for the fork tests plus the affected title-generator,
topic-mode, title-command, rename-lane, native topic opener and platform-actions
suites. Native observer tests require the repository-pinned Telegram SDK, not a
production bot token. Runtime activation/deployment and any service restart are
separate operations, not consequences of running these tests.
