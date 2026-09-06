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

_HOTSPOT_RE = re.compile(r"^[ \t>*\-]*(?:\*\*)?hotspot(?:\*\*)?[ \t]*:[ \t]*(?P<rest>\S.*)$", re.IGNORECASE)
# A worker writes "hotspot: <path> — <reason>". The separator is an em/en dash or
# a spaced ASCII dash; requiring the surrounding space keeps a hyphenated
# directory name ("right-rail/preview-pane.tsx") intact.
_HOTSPOT_REASON_RE = re.compile(r"\s+(?:[—–]|--|-)\s+")

_STRIP_CHARS = "`'\"*<>()[],;:."

# The worker protocol asks EVERY card for a hotspot line, so the overwhelmingly
# common value is a negation — usually followed by prose that happens to name
# files ("none. The three files I touched (a.py, b.py) showed no collision").
# Mining that prose parks two unrelated cards behind each other, which is worse
# than missing a real hotspot: a false negative costs nothing, a false
# serialization stalls a card the operator never asked to gate.
_NEGATIONS = frozenset(
    {
        "none", "no", "n/a", "na", "nil", "nope", "nothing", "not applicable",
        "none yet", "none known", "none found", "none identified", "none observed",
        "no collision", "no collisions", "no overlap", "no overlaps",
        "no hotspot", "no hotspots", "no conflict", "no conflicts",
    }
)
# First clause of a payload: everything before the first sentence/list break.
_HEAD_CLAUSE_RE = re.compile(r"[.;,]|\s+(?:[—–]|--|-)\s+")
# Glob metacharacters other than braces. Matching is exact per-path equality, so
# a pattern can never be a holder key — it would claim files it does not name.
_GLOB_CHARS = "*?[]"
# Brace members that are an elision, not a filename: "{en,types,...}.ts".
_ELISION_MEMBERS = frozenset({"...", "…", "..", "etc", "etc.", "…etc"})


def normalize_edit_path(raw: Optional[str]) -> str:
    """Canonical form of one declared path, or ``""`` when it isn't one.

    Equivalent spellings of the same file must compare equal or the guard is
    trivially defeated by formatting: ``./a/b.ts``, ``a//b.ts`` and `` `a/b.ts` ``
    all normalize to ``a/b.ts``. Case is preserved — these are POSIX paths.

    Rejects anything still carrying glob syntax (``*``, ``?``, ``[]``, or a
    leftover brace). Those are patterns, not paths; admitting one would let a
    fragment such as ``apps/src/i18n/{en`` become a holder key that two
    unrelated cards then "collide" on.
    """
    text = (raw or "").strip().strip(_STRIP_CHARS).strip()
    if not text or any(ch.isspace() for ch in text):
        return ""
    if any(ch in text for ch in _GLOB_CHARS) or "{" in text or "}" in text:
        return ""
    # A bare word is prose, not a path; require a directory separator or a suffix.
    if "/" not in text and "." not in text:
        return ""
    text = text.replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    text = re.sub(r"/{2,}", "/", text).strip("/")
    return text


def is_negation(payload: Optional[str]) -> bool:
    """True when a declared/hotspot payload says "nothing here".

    Only the first clause is consulted: ``none. The files I touched (a.py, b.py)``
    is a negation whose trailing prose must never be mined for paths.
    """
    text = (payload or "").strip().strip(_STRIP_CHARS).strip().lower()
    if not text:
        return True
    head = _HEAD_CLAUSE_RE.split(text, maxsplit=1)[0]
    head = head.strip().strip(_STRIP_CHARS).strip()
    return head in _NEGATIONS or text in _NEGATIONS


def expand_brace_path(text: str) -> list[str]:
    """Expand ``a/{x,y}.ts`` into ``['a/x.ts', 'a/y.ts']``; ``[]`` if unbalanced.

    Real board text carries brace expansions
    (``apps/desktop/src/i18n/{en,types,zh}.ts``). A naive comma split cuts them
    into fragments — ``apps/desktop/src/i18n/{en`` and ``zh}.ts`` — and the
    prefix fragment is identical for any two cards touching that directory, so
    they serialize on a path that does not exist. Expanding yields the real
    per-file names, which then compare exactly like any other path; an
    unbalanced brace yields nothing rather than a fragment.
    """
    if "{" not in text and "}" not in text:
        return [text]
    if text.count("{") != text.count("}"):
        return []
    start = text.find("{")
    depth = 0
    end = -1
    for pos in range(start, len(text)):
        if text[pos] == "{":
            depth += 1
        elif text[pos] == "}":
            depth -= 1
            if depth == 0:
                end = pos
                break
    if end < 0:
        # A '}' precedes the first '{' ("ar}.ts{"): not a brace group.
        return []
    prefix, body, suffix = text[:start], text[start + 1:end], text[end + 1:]
    out: list[str] = []
    for member in body.split(","):
        member = member.strip()
        if not member or member.lower() in _ELISION_MEMBERS:
            continue
        for expanded in expand_brace_path(prefix + member + suffix):
            if expanded not in out:
                out.append(expanded)
    return out


def _split_payload(payload: str) -> list[str]:
    """Split on ``,``/``;``/``|`` that are OUTSIDE a brace group."""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in payload:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(depth - 1, 0)
        elif ch in ",;|" and depth == 0:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    parts.append("".join(buf))
    return parts


def _split_paths(payload: str, *, strict: bool = False) -> list[str]:
    """Normalized paths from one separated payload, brace groups kept intact.

    ``strict`` makes one non-path chunk reject the WHOLE payload. Hotspot lines
    use it: the protocol asks every worker for one, so a line whose chunks are
    not all paths is prose and must contribute nothing rather than donating the
    few chunks that happen to look like filenames.
    """
    out: list[str] = []
    for chunk in _split_payload(payload):
        if not chunk.strip():
            continue
        candidates = expand_brace_path(chunk)
        if not candidates:
            if strict:
                return []
            continue
        for candidate in candidates:
            path = normalize_edit_path(candidate)
            if not path:
                if strict:
                    return []
                continue
            if path not in out:
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
            rest = inline.group("rest")
            # "Edit-Targets: none" is a declaration that there is nothing to
            # serialize on, not a path called "none"/"N/A".
            if not is_negation(rest):
                for path in _split_paths(rest):
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
                item = bullet.group("item")
                if not is_negation(item):
                    for path in _split_paths(item):
                        if path not in found:
                            found.append(path)
                index += 1
            continue
        index += 1
    return found


def parse_hotspot_paths(text: Optional[str]) -> list[str]:
    """Paths from ``hotspot: <path> — <reason>`` lines in a comment body.

    Deliberately strict. The worker protocol asks EVERY card for a hotspot line,
    so most lines on the board are negations followed by explanatory prose
    ("none. The three files I touched (a.py, b.py) showed no collision"). Mining
    that prose serializes two unrelated cards — a much worse failure than
    missing a genuine hotspot, because a card the operator never asked to gate
    stops moving. So: a negated line contributes nothing, and a line whose
    comma-separated chunks are not ALL paths contributes nothing rather than
    donating the chunks that happen to look like filenames.
    """
    if not text:
        return []
    found: list[str] = []
    for line in text.splitlines():
        match = _HOTSPOT_RE.match(line)
        if not match:
            continue
        rest = match.group("rest")
        if is_negation(rest):
            continue
        payload = _HOTSPOT_REASON_RE.split(rest, maxsplit=1)[0]
        for path in _split_paths(payload, strict=True):
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
            if is_negation(item):
                continue
            # Accept both a bare path and the "<path> — <reason>" prose form,
            # under the same strict rule as a comment line: a value that is not
            # wholly paths is prose and contributes nothing.
            payload = _HOTSPOT_REASON_RE.split(item, maxsplit=1)[0]
            candidates = _split_paths(payload, strict=True) or parse_hotspot_paths(item)
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


# A holder in one of these statuses will not reach ``done`` under its own power:
# it is waiting on a human. Anything else (todo/ready/running/review/scheduled/
# triage) is still moving through the pipeline, and ``done``/``archived`` already
# satisfy ``recompute_ready``, so only these strand a parked card.
_STALLED_HOLDER_STATUSES = ("blocked", "on_hold")


def _edge_event_counts(
    conn: sqlite3.Connection, child_id: str, holder_id: str
) -> tuple[int, int]:
    """``(linked_count, serialized_count)`` for the ``holder -> child`` edge.

    Payloads are JSON-decoded rather than pattern-matched: a ``LIKE`` on
    serialized JSON breaks the moment the encoder's spacing changes.
    """
    linked = serialized = 0
    for row in conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? "
        "AND kind IN ('linked', 'serialized_coedit')", (child_id,),
    ):
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        if row["kind"] == "linked" and payload.get("parent") == holder_id:
            linked += 1
        elif row["kind"] == "serialized_coedit" and payload.get("holder") == holder_id:
            serialized += 1
    return linked, serialized


def _coedit_edge_is_ours(conn: sqlite3.Connection, child_id: str, holder_id: str) -> bool:
    """True when the dispatcher is the ONLY author of the ``holder -> child`` edge.

    ``link_tasks`` appends a ``linked`` event for every link it makes, and the
    guard appends exactly one ``serialized_coedit`` beside each link it made. If
    an orchestrator or a human also linked this pair, there is a ``linked``
    event with no ``serialized_coedit`` to account for it — that edge expresses
    a real dependency and releasing it would drop work ordering the board asked
    for. Count-equality is the conservative reading: when in doubt, keep it.
    """
    linked, serialized = _edge_event_counts(conn, child_id, holder_id)
    return serialized > 0 and linked == serialized


def release_stranded_coedit_edges(conn: sqlite3.Connection) -> list:
    """Drop dispatcher-added serialization edges whose holder has stalled.

    The guard's whole argument is that "needs a human" is a routing bug for an
    unattended fleet. A permanent edge would recreate exactly that: a holder
    that goes ``blocked`` (or is auto-blocked by the breaker after failing)
    holds an unrelated card hostage until an operator unblocks a *different*
    card. ``recompute_ready`` promotes only when every parent is done, so
    nothing else would ever free it.

    So the edge is a lease, not a dependency: it lasts exactly as long as the
    holder is still on its way to producing the work the parked card should
    start from. Returns ``(child_id, holder_id)`` for each edge released.
    """
    from hermes_cli import kanban_db as _kb

    placeholders = ",".join("?" for _ in _STALLED_HOLDER_STATUSES)
    rows = conn.execute(
        "SELECT DISTINCT e.task_id AS child_id, l.parent_id AS holder_id, "
        "holder.status AS holder_status "
        "FROM task_events e "
        "JOIN task_links l ON l.child_id = e.task_id "
        "JOIN tasks holder ON holder.id = l.parent_id "
        "JOIN tasks child ON child.id = e.task_id "
        "WHERE e.kind = 'serialized_coedit' "
        f"AND holder.status IN ({placeholders}) "
        "AND child.status NOT IN ('done', 'archived') "
        "ORDER BY e.task_id, l.parent_id",
        _STALLED_HOLDER_STATUSES,
    ).fetchall()
    released: list = []
    for row in rows:
        child_id, holder_id = row["child_id"], row["holder_id"]
        if not _coedit_edge_is_ours(conn, child_id, holder_id):
            continue
        # unlink_tasks appends the ``unlinked`` event and re-runs
        # ``recompute_ready``, so the freed card promotes in this same tick.
        if _kb.unlink_tasks(conn, holder_id, child_id):
            with _kb.write_txn(conn):
                _kb._append_event(
                    conn, child_id, "coedit_released",
                    {"holder": holder_id, "holder_status": row["holder_status"]},
                )
            released.append((child_id, holder_id))
    return released
