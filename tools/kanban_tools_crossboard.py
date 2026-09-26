"""Cross-board reads for ``kanban_list`` / ``kanban_show`` (``board="*"``).

Each board is opened read-only at its canonical path (``default`` at
``<kanban_home>/kanban.db``, named boards under ``boards/<slug>/``), never via
``connect()``: a scan must not init, migrate, or quarantine a board it merely
reads, and an inherited ``HERMES_KANBAN_DB`` pin must not alias ``default``.
Failures are isolated per board and reported as ``board_errors``.
"""
from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

ALL_BOARDS = "*"
READ_TOOLS = frozenset({"kanban_list", "kanban_show"})


def is_all(board: Any) -> bool:
    return isinstance(board, str) and board.strip() == ALL_BOARDS


def star_refusal(tool_name: str) -> Optional[str]:
    """Error text when a non-read tool is handed ``board="*"``; else None."""
    if tool_name in READ_TOOLS:
        return None
    return (f'{tool_name}: board="*" (all boards) is only accepted by the read-only '
            "kanban_list and kanban_show; pass a single board slug.")


def _board_db_paths(kb) -> list[tuple[str, Path]]:
    out = []
    for meta in kb.list_boards(include_archived=False):
        slug = str(meta["slug"])
        root = kb.kanban_home() if slug == kb.DEFAULT_BOARD else kb.board_dir(slug)
        out.append((slug, root / "kanban.db"))
    return out


@contextlib.contextmanager
def _open_readonly(path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        has_schema = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tasks'").fetchone()
        if not has_schema:
            raise ValueError("no kanban schema")
        yield conn
    finally:
        conn.close()


def read_each_board(read: Callable[[Any, sqlite3.Connection, str], Any]):
    """``read(kb, conn, slug)`` on every non-archived board with a DB on disk.
    Returns ``([(slug, result), ...], [{"board", "error"}, ...])`` in slug order."""
    from hermes_cli import kanban_db as kb
    results, errors = [], []
    for slug, path in sorted(_board_db_paths(kb)):
        if not path.exists():
            continue  # board.json-only or never-initialised default: nothing to read
        try:
            with _open_readonly(path) as conn:
                results.append((slug, read(kb, conn, slug)))
        except Exception as e:
            errors.append({"board": slug, "error": f"{type(e).__name__}: {e}"})
    return results, errors


def list_all(*, assignee, status, tenant, include_archived: bool, limit: int, max_limit: int,
             summarize: Callable) -> dict[str, Any]:
    """Merged rows from every board, capped at ``limit`` in total; order is
    priority desc, then board slug, then each board's own ``list_tasks`` order.
    No ``recompute_ready`` pass: that is a write, and this path only reads."""
    def read(kb, conn, slug):
        # limit+1 per board suffices: the global top limit+1 can't need more from one board.
        rows = kb.list_tasks(conn, assignee=assignee, status=status, tenant=tenant,
                             include_archived=include_archived, limit=limit + 1)
        return [{**summarize(kb, conn, t), "board": slug} for t in rows]

    per_board, errors = read_each_board(read)
    merged = [(-(row.get("priority") or 0), slug, i, row)
              for slug, rows in per_board for i, row in enumerate(rows)]
    merged.sort(key=lambda item: item[:3])
    truncated = len(merged) > limit
    tasks = [row for *_, row in merged[:limit]]
    return {
        "tasks": tasks, "count": len(tasks), "limit": limit, "truncated": truncated,
        "next_limit": min(limit * 2, max_limit) if truncated and limit < max_limit else None,
        "boards": [slug for slug, _ in per_board], "board_errors": errors}


def find_task(tid: str, payload: Callable[[Any, sqlite3.Connection, str], dict]):
    """``(hits, errors)``: ``payload(kb, conn, slug)`` for each board holding ``tid``."""
    def read(kb, conn, slug):
        return payload(kb, conn, slug) if kb.get_task(conn, tid) is not None else None

    results, errors = read_each_board(read)
    return [(slug, out) for slug, out in results if out is not None], errors
