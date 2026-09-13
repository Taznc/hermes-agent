"""Per-session archive / unarchive / batch delete for ``hermes sessions``.

The filter-driven bulk selector lives in :func:`hermes_cli.sessions_cmd._cmd_prune_or_archive`.
This sibling owns the id-driven half of the same commands: an operator (or an agent) that has
already decided *which* sessions to curate names them outright instead of describing them.

Shape decisions (mirrored in ``hermes_cli/subcommands/sessions.py``):

* ``archive`` is filter-first, so its id mode is the explicit ``--ids A B C`` and mixing the two
  is rejected — a half-filter/half-id selection is never what the caller meant.
* ``unarchive`` has no filter mode, so it accepts ids positionally (matching ``pin`` / ``unpin``)
  and via ``--ids`` for symmetry with ``archive --ids``.
* Both reach the store through ``SessionDB.set_session_archived``, so a compression lineage flips
  as a unit and an OPEN session (``ended_at IS NULL``) archives like any other — the id path never
  had a reason to inherit the bulk selector's ended-only gate.
"""

from __future__ import annotations

from hermes_cli.sessions_cmd import _any_filter_args, _confirm_prompt, _sessions_dir

#: Filter flags that make no sense alongside an explicit id list, with the flag spelling to quote.
_FILTER_FLAG_HINT = "--older-than/--newer-than/--before/--after/--source/--title/--model/..."


def _collect_ids(args) -> list:
    """Ids from the positional slot and/or ``--ids``, in order, de-duplicated.

    ``session_id`` (singular) is read too: programmatic callers build the Namespace by hand and
    the delete/rename handlers historically took one id under that name.
    """
    single = getattr(args, "session_id", None)
    raw = [*(getattr(args, "session_ids", None) or []), *(getattr(args, "ids", None) or []),
           *([single] if single else [])]
    seen, out = set(), []
    for value in raw:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _describe(db, session_id: str) -> str:
    """One preview line: id, source, message count, title (best effort — a racing delete is fine)."""
    row = db.get_session(session_id) or {}
    open_tag = "open" if row.get("ended_at") is None else "ended"
    title = (row.get("title") or "")[:40]
    return (f"  {session_id}  {(row.get('source') or '-'):<10} {open_tag:<6} "
            f"{row.get('message_count', 0):>4} msgs  {title}")


def _report_unknown(unknown) -> None:
    for raw in unknown:
        print(f"Session '{raw}' not found.")


def cmd_archive_ids(db, args, archived: bool):
    """``sessions archive --ids ...`` / ``sessions unarchive ...``.

    Unknown ids are reported and cost a non-zero exit, but the ids that *did* resolve are still
    acted on (same partial-success contract as ``sessions pin``): a curation run over a stale list
    should not be all-or-nothing.
    """
    verb, verbed = ("Archive", "Archived") if archived else ("Unarchive", "Unarchived")
    if archived and _any_filter_args(args):
        print(f"Error: --ids cannot be combined with metadata filters ({_FILTER_FLAG_HINT}).\n"
              "Archive by id, or by filter — not both.")
        return 1
    raw_ids = _collect_ids(args)
    if not raw_ids:
        print(f"Error: no session ids given. Usage: hermes sessions "
              f"{'archive --ids' if archived else 'unarchive'} <session_id> [<session_id> ...]")
        return 1
    if archived and getattr(args, "include_open", False):
        print("Note: --include-open is implied by --ids (an id names one session regardless of "
              "whether it has ended); the flag had no effect.")

    resolved, unknown = db.resolve_session_ids(raw_ids)
    _report_unknown(unknown)
    if not resolved:
        return 1

    if getattr(args, "dry_run", False) or not getattr(args, "yes", False):
        print(f"{len(resolved)} session(s) would be {verbed.lower()}:")
        for session_id in resolved:
            print(_describe(db, session_id))
    if getattr(args, "dry_run", False):
        print(f"Dry run — nothing {verbed.lower()}.")
        return 1 if unknown else None
    if not getattr(args, "yes", False) and not _confirm_prompt(
        f"{verb} these {len(resolved)} session(s)? [y/N] "
    ):
        print("Cancelled.")
        return 1 if unknown else None

    flipped = db.set_sessions_archived(resolved, archived)
    if archived:
        print(f"Archived {len(flipped)} session(s). They're hidden from listings but fully "
              "recoverable (nothing was deleted) — `hermes sessions unarchive <id>` to restore.")
    else:
        print(f"Unarchived {len(flipped)} session(s). They're back in `hermes sessions list`.")
    return 1 if unknown else None


def cmd_delete_ids(db, args):
    """``sessions delete <id> [<id> ...]``: batch delete with the single-id confirm semantics.

    One id keeps the exact prompt/output it always had; several share one confirmation. A pin is a
    durable "keep" flag, so a pinned target is called out rather than silently destroyed.
    """
    raw_ids = _collect_ids(args)
    if not raw_ids:
        print("Error: no session ids given. Usage: hermes sessions delete <session_id> "
              "[<session_id> ...]")
        return 1
    resolved, unknown = db.resolve_session_ids(raw_ids)
    _report_unknown(unknown)
    if not resolved:
        return 1
    pinned = [sid for sid in resolved if (db.get_session(sid) or {}).get("pinned")]

    if getattr(args, "dry_run", False):
        print(f"{len(resolved)} session(s) would be deleted:")
        for session_id in resolved:
            print(_describe(db, session_id))
        print("Dry run — nothing deleted.")
        return 1 if unknown else None

    single = resolved[0] if len(resolved) == 1 else None
    pinned_note = " (this session is PINNED)" if single and pinned else ""
    if not args.yes:
        prompt = (f"Delete session '{single}'{pinned_note} and all its messages? [y/N] " if single
                  else f"Delete these {len(resolved)} session(s) and all their messages"
                       f"{f' ({len(pinned)} PINNED)' if pinned else ''}? [y/N] ")
        if not _confirm_prompt(prompt):
            print("Cancelled.")
            return 1 if unknown else None
    elif pinned:
        for session_id in pinned:
            print(f"Warning: deleting a pinned session '{session_id}'.")

    deleted = [sid for sid in resolved if db.delete_session(sid, sessions_dir=_sessions_dir())]
    if single:
        # Preserve the historical single-id wording exactly; scripts and tests read it.
        if not deleted:
            print(f"Session '{raw_ids[0]}' not found.")
            return 1
        print(f"Deleted session '{single}'.")
    else:
        print(f"Deleted {len(deleted)} session(s).")
    return 1 if unknown or len(deleted) != len(resolved) else None
