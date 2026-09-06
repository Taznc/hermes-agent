"""Co-edit serialization: read back the edit surface a card declares (or has
already collided on) so the dispatcher can serialize two cards that would
otherwise edit the same file concurrently.

Why this exists
---------------
Two cards were fanned out at the same time, both touching
``apps/desktop/src/app/chat/right-rail/preview-pane.tsx`` and both needing the
same new helper. The shared decision was written into BOTH card bodies as prose
("import it, do not define a second one") and it still failed: the workers
finished nine minutes apart, neither could see the other's worktree, and both
created the helper with different bodies — an add/add conflict plus a content
conflict, costing a review cycle and a rework card.

Prose cannot serialize two agents who cannot see each other. The board's
``parents=[...]`` edge is the only mechanism that can, so the dispatcher turns
an overlapping edit surface into a real dependency edge: the second card waits
and later starts from a tree that already contains the first card's work.

Two deterministic signals, no prose inference and no static analysis:

1. **Declared** — a card body carries an ``Edit-Targets:`` field the orchestrator
   fills in when fanning out. This works on the FIRST dispatch, before any
   worker has collided.
2. **Emitted** — a worker that hit a collision files a
   ``hotspot: <path> — <reason>`` comment (the worker protocol asks for it) and
   reviewers put the same under a ``hotspot``/``hotspots`` key in completion
   metadata. That signal was already in the DB and simply never read back.

Everything here is exact-path equality after normalization. No globs, no
directory prefixes, no repo-wide lock: only the genuinely overlapping pair is
serialized, and a card that declares nothing behaves exactly as it did before.
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Iterable
from typing import Optional

# Labels accepted for the declared-edit-surface field, canonical spelling first.
# Matched case-insensitively with ``-``/space interchangeable, so an orchestrator
# writing "Edit targets:" is not silently ignored.
_DECLARED_LABELS = (
    "edit targets",
    "edit target",
    "declared paths",
    "declared path",
    "edit surface",
)

_LABEL_ALTERNATION = "|".join(
    lbl.replace(" ", r"[ \-_]") for lbl in _DECLARED_LABELS
)

# "Edit-Targets: a/b.ts, c/d.tsx" — the rest of the line is the payload.
_INLINE_RE = re.compile(
    rf"^[ \t>*\-]*(?:\*\*)?(?:{_LABEL_ALTERNATION})(?:\*\*)?[ \t]*:[ \t]*(?P<rest>\S.*)$",
    re.IGNORECASE,
)
# "Edit targets:" alone on a line, followed by a bullet list.
_HEADING_RE = re.compile(
    rf"^[ \t>*\-#]*(?:\*\*)?(?:{_LABEL_ALTERNATION})(?:\*\*)?[ \t]*:?[ \t]*$",
    re.IGNORECASE,
)
_BULLET_RE = re.compile(r"^[ \t>]*(?:[-*+]|\d+[.)])[ \t]+(?P<item>\S.*?)[ \t]*$")

_HOTSPOT_RE = re.compile(r"^[ \t>*\-]*hotspot[ \t]*:[ \t]*(?P<rest>\S.*)$", re.IGNORECASE)
# A worker writes "hotspot: <path> — <reason>". The separator is an em/en dash or
# a spaced ASCII dash; requiring the surrounding space keeps a hyphenated
# directory name ("right-rail/preview-pane.tsx") intact.
_HOTSPOT_REASON_RE = re.compile(r"\s+(?:[—–]|--|-)\s+")

_STRIP_CHARS = "`'\"*<>()[],;:."


def normalize_edit_path(raw: Optional[str]) -> str:
    """Canonical form of one declared path, or ``""`` when it isn't one.

    Equivalent spellings of the same file must compare equal or the guard is
    trivially defeated by formatting: ``./a/b.ts``, ``a//b.ts`` and `` `a/b.ts` ``
    all normalize to ``a/b.ts``. Case is preserved — these are POSIX paths.
    """
    text = (raw or "").strip().strip(_STRIP_CHARS).strip()
    if not text or any(ch.isspace() for ch in text):
        return ""
    # A bare word is prose, not a path; require a directory separator or a suffix.
    if "/" not in text and "." not in text:
        return ""
    text = text.replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    text = re.sub(r"/{2,}", "/", text).strip("/")
    return text


def _split_paths(payload: str) -> list[str]:
    """Normalized paths from one comma/semicolon/pipe-separated payload."""
    out: list[str] = []
    for chunk in re.split(r"[,;|]", payload):
        path = normalize_edit_path(chunk)
        if path and path not in out:
            out.append(path)
    return out


def parse_declared_paths(body: Optional[str]) -> list[str]:
    """Paths a card body declares up front, in first-seen order.

    Accepts the inline form (``Edit-Targets: a/b.ts, c/d.tsx``) and the heading
    plus bullet-list form. A body with no such field yields ``[]`` — that is the
    common case and it must stay a no-op.
    """
    if not body:
        return []
    found: list[str] = []
    lines = body.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        inline = _INLINE_RE.match(line)
        if inline:
            for path in _split_paths(inline.group("rest")):
                if path not in found:
                    found.append(path)
            index += 1
            continue
        if _HEADING_RE.match(line):
            index += 1
            # Consume the bullet list that follows, tolerating blank lines
            # between the heading and the first bullet.
            while index < len(lines):
                nxt = lines[index]
                if not nxt.strip():
                    index += 1
                    continue
                bullet = _BULLET_RE.match(nxt)
                if not bullet:
                    break
                for path in _split_paths(bullet.group("item")):
                    if path not in found:
                        found.append(path)
                index += 1
            continue
        index += 1
    return found


def parse_hotspot_paths(text: Optional[str]) -> list[str]:
    """Paths from ``hotspot: <path> — <reason>`` lines in a comment body."""
    if not text:
        return []
    found: list[str] = []
    for line in text.splitlines():
        match = _HOTSPOT_RE.match(line)
        if not match:
            continue
        payload = _HOTSPOT_REASON_RE.split(match.group("rest"), maxsplit=1)[0]
        for path in _split_paths(payload):
            if path not in found:
                found.append(path)
    return found


def _metadata_hotspot_paths(raw: Optional[str]) -> list[str]:
    """Paths under a ``hotspot``/``hotspots`` key of a run's completion metadata.

    Reviewers and workers put the collision there as well as in a comment; the
    value may be a single string, a list, or the same ``<path> — <reason>``
    prose a comment carries.
    """
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    found: list[str] = []
    for key in ("hotspot", "hotspots", "hotspot_paths"):
        value = data.get(key)
        if value is None:
            continue
        items = value if isinstance(value, (list, tuple)) else [value]
        for item in items:
            if not isinstance(item, str):
                continue
            # Accept both a bare path and the "<path> — <reason>" prose form.
            payload = _HOTSPOT_REASON_RE.split(item, maxsplit=1)[0]
            candidates = _split_paths(payload) or parse_hotspot_paths(item)
            for path in candidates:
                if path not in found:
                    found.append(path)
    return found


def edit_paths_for_tasks(
    conn: sqlite3.Connection, task_ids: Iterable[str]
) -> "dict[str, list[str]]":
    """Bulk-load every task's edit surface: declared field + emitted hotspots.

    One query per signal rather than per task — the dispatcher calls this on
    every tick, so it must not scale with board size.
    """
    ordered = list(dict.fromkeys(str(t) for t in task_ids if t))
    paths: dict[str, list[str]] = {task_id: [] for task_id in ordered}
    if not ordered:
        return paths

    def _extend(task_id: str, new: Iterable[str]) -> None:
        bucket = paths.get(task_id)
        if bucket is None:
            return
        for path in new:
            if path not in bucket:
                bucket.append(path)

    placeholders = ",".join("?" for _ in ordered)
    params = tuple(ordered)
    for row in conn.execute(
        f"SELECT id, body FROM tasks WHERE id IN ({placeholders})", params
    ):
        _extend(row["id"], parse_declared_paths(row["body"]))
    for row in conn.execute(
        f"SELECT task_id, body FROM task_comments WHERE task_id IN ({placeholders}) "
        "AND body LIKE '%hotspot:%' ORDER BY created_at",
        params,
    ):
        _extend(row["task_id"], parse_hotspot_paths(row["body"]))
    for row in conn.execute(
        f"SELECT task_id, metadata FROM task_runs WHERE task_id IN ({placeholders}) "
        "AND metadata LIKE '%hotspot%' ORDER BY started_at",
        params,
    ):
        _extend(row["task_id"], _metadata_hotspot_paths(row["metadata"]))
    return paths


def edit_paths_for_task(conn: sqlite3.Connection, task_id: str) -> list[str]:
    """Edit surface of a single task (declared field + emitted hotspots)."""
    return edit_paths_for_tasks(conn, [task_id])[task_id]


class CoeditIndex:
    """``path -> holder task id`` for the cards that currently own an edit surface.

    Seeded from the tasks already ``running`` and extended as this tick spawns
    more, so two ready cards fanned out together are serialized against each
    other and not only against a previous tick's work. First claimant of a path
    wins; a later card that names it is parked behind that holder.

    ``tenant`` scoping matters: tenants are separate workspaces on one board, so
    two tenants naming the same repo-relative path are not co-editing anything.
    """

    __slots__ = ("_holders",)

    def __init__(self, holders: Optional[dict] = None) -> None:
        # (tenant, path) -> holder task id
        self._holders: dict[tuple, str] = dict(holders or {})

    def __bool__(self) -> bool:
        return bool(self._holders)

    def holder_for(self, tenant: Optional[str], paths: Iterable[str]) -> Optional[tuple]:
        """First ``(holder_id, path)`` this card would collide with, else None."""
        for path in paths:
            holder = self._holders.get((tenant or "", path))
            if holder:
                return holder, path
        return None

    def claim(self, task_id: str, tenant: Optional[str], paths: Iterable[str]) -> None:
        """Record ``task_id`` as the holder of every path it does not already share."""
        for path in paths:
            self._holders.setdefault((tenant or "", path), task_id)


def build_coedit_index(conn: sqlite3.Connection) -> CoeditIndex:
    """Index the edit surface owned by every currently-``running`` card.

    Returns an empty index when nothing running declares anything, which is the
    common case and makes the whole guard a no-op.
    """
    rows = conn.execute(
        "SELECT id, tenant FROM tasks WHERE status = 'running'"
    ).fetchall()
    if not rows:
        return CoeditIndex()
    tenants = {row["id"]: row["tenant"] for row in rows}
    paths_by_task = edit_paths_for_tasks(conn, tenants.keys())
    index = CoeditIndex()
    # Deterministic order so two dispatchers racing on the same board agree on
    # which card holds a contended path.
    for task_id in sorted(tenants):
        index.claim(task_id, tenants[task_id], paths_by_task.get(task_id, []))
    return index
