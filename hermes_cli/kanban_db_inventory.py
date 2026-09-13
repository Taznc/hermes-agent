"""Cross-process lock over the kanban **board inventory** — the set of boards that exist.

``hermes_cli.kanban_db`` serializes writes *within* one board with SQLite
(``BEGIN IMMEDIATE`` + WAL). Nothing serialized the set of boards itself, so a
fleet-wide reader that wants to enumerate every board and lock each one has no
way to stop a board from being created, archived, deleted or imported halfway
through its sweep — it would lock a board that no longer exists and miss one
that appeared a millisecond after ``list_boards()`` returned.

:func:`board_inventory_lock` is that missing seam. Every operation that adds,
removes or replaces a board-directory entry takes it, so a reader holding it
sees a frozen inventory:

* :func:`hermes_cli.kanban_db.write_board_metadata` (creates ``board.json`` for
  an absent slug; also the existing-board edit path — one re-entrant primitive)
* :func:`hermes_cli.kanban_db.create_board`
* :func:`hermes_cli.kanban_db.remove_board` (archive and delete)
* :func:`hermes_cli.kanban_transfer.import_board` (target-slug selection
  through final placement + metadata)
* :func:`hermes_cli.kanban_db_connect.connect` and
  :func:`hermes_cli.kanban_db_connect.init_db` **when, and only when, the named
  board is absent** — their auto-init creates ``boards/<slug>/kanban.db``, which
  is what makes that board visible to :func:`list_boards`. Their existing-board
  fast path uses SQLite's no-create mode and retries under this lock if removal
  wins the race after the existence check.

Reads take nothing: ``list_boards()`` / ``read_board_metadata()`` stay
lock-free, so an unlocked reader is never blocked by an unrelated create. Nor
is a connection to a board that already exists: opening one changes no
inventory entry, and gating every connect on this lock would let a single
fleet reader freeze every board read in the fleet.

**Lock ordering.** The inventory lock is strictly OUTERMOST: inventory lock ->
per-board SQLite locks (``_cross_process_init_lock``, ``BEGIN IMMEDIATE``).
No path may take a board DB lock and then reach for the inventory lock; the
consumer this exists for (the fleet safety reader) holds the inventory lock
around its per-board ``BEGIN IMMEDIATE`` holds, which is the same direction.

**Mechanism.** POSIX ``fcntl.flock`` / Windows ``msvcrt.locking`` on a single
file derived from the canonical profile-aware boards root, reusing the helpers
in :mod:`hermes_cli.kanban_db_connect`. Kernel-managed, so a holder that dies
releases it — no stale-lock reaping, no pid files. An in-process ``RLock`` plus
a per-thread depth counter makes it re-entrant for the same thread (``create_board``
nests ``write_board_metadata``) while still excluding sibling threads exactly
like sibling processes: POSIX ``flock`` is per open-file-description, so a
second fd in the same process would deadlock against itself without this.

**Bounded, and fails CLOSED.** Every acquisition has a deadline and raises
:class:`BoardInventoryLockTimeout` when it expires. That is deliberately unlike
``_cross_process_init_lock``, which proceeds *without* the lock on timeout
because init is idempotent — here, proceeding unlocked is precisely the race
being guarded.
"""

from __future__ import annotations

import contextlib
import threading
import time
from pathlib import Path
from typing import Iterator, Optional

# Default acquisition bound. A bare blocking flock let a wedged holder freeze
# the dispatcher's next-tick connect forever (#36644); an unbounded default
# here would do the same to every board create/remove/import.
DEFAULT_INVENTORY_LOCK_TIMEOUT_SECONDS = 30.0
_POLL_SECONDS = 0.02

# Name of the lock file, placed BESIDE the boards directory rather than inside
# it: ``remove_board``'s rename into ``boards/_archived/`` and ``list_boards()``'s
# directory walk must never see or move the lock.
_LOCK_FILENAME = "boards.lock"

_INVENTORY_RLOCK = threading.RLock()
_DEPTH = threading.local()


class BoardInventoryLockTimeout(TimeoutError):
    """The board-inventory lock was not acquired within the requested timeout.

    Raised instead of continuing unlocked: the caller was about to change which
    boards exist, and doing that without exclusion is the race
    :func:`board_inventory_lock` exists to prevent.
    """

    def __init__(self, path: Path, timeout: float):
        self.path = path
        self.timeout = timeout
        super().__init__(
            f"board inventory lock {str(path)!r} is held by another process "
            f"(waited {timeout:g}s); the board inventory was not changed"
        )


def _lock_path() -> Path:
    """The one lock file, derived from the canonical profile-aware boards root.

    Callers never build this path — ``boards_root()`` is the single source of
    board identity, so two processes resolving different kanban homes correctly
    get different (non-excluding) locks.
    """
    from hermes_cli import kanban_db as _kb

    return _kb.boards_root().parent / _LOCK_FILENAME


def _resolve_deadline(timeout: Optional[float]) -> tuple[float, float]:
    """``(budget_seconds, monotonic_deadline)`` for ``timeout``."""
    budget = DEFAULT_INVENTORY_LOCK_TIMEOUT_SECONDS if timeout is None else float(timeout)
    budget = max(0.0, budget)
    return budget, time.monotonic() + budget


@contextlib.contextmanager
def board_inventory_lock(timeout: Optional[float] = None) -> Iterator[None]:
    """Hold the board-inventory lock for the duration of the ``with`` block.

    ``timeout`` is a bound in seconds on *acquisition* only (the hold itself is
    unbounded): ``None`` uses :data:`DEFAULT_INVENTORY_LOCK_TIMEOUT_SECONDS`,
    ``0`` makes a single non-blocking attempt, negatives are clamped to ``0``.
    Exhausting it raises :class:`BoardInventoryLockTimeout` having changed
    nothing. Re-entrant within one thread; excludes every other thread and
    process.
    """
    budget, deadline = _resolve_deadline(timeout)

    depth = getattr(_DEPTH, "n", 0)
    if depth:  # already held by this thread — the inner block is a no-op
        _DEPTH.n = depth + 1
        try:
            yield
        finally:
            _DEPTH.n = depth
        return

    path = _lock_path()
    if not _acquire_rlock(deadline):
        raise BoardInventoryLockTimeout(path, budget)
    try:
        handle = _open_lock_file(path)
        try:
            _acquire_file_lock(handle, path, budget, deadline)
        except BaseException:
            handle.close()
            raise
        _DEPTH.n = 1
        try:
            yield
        finally:
            _DEPTH.n = 0
            try:
                _release_file_lock(handle)
            finally:
                handle.close()
    finally:
        _INVENTORY_RLOCK.release()


def _acquire_rlock(deadline: float) -> bool:
    """Take the in-process lock without overrunning ``deadline``."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return _INVENTORY_RLOCK.acquire(blocking=False)
    return _INVENTORY_RLOCK.acquire(timeout=remaining)


def path_is_board_entry(db_path: Path) -> bool:
    """Whether ``db_path`` has the canonical named-board inventory shape.

    This is intentionally independent of current existence: callers use it to
    decide whether an existing-only open that loses a removal race must retry
    under :func:`board_inventory_lock`.
    """
    from hermes_cli import kanban_db as _kb

    board_dir = db_path.parent
    return db_path.name == "kanban.db" and board_dir.parent == _kb.boards_root()


def path_is_new_board_entry(db_path: Path) -> bool:
    """True when opening ``db_path`` would ADD a board-directory entry.

    ``connect``/``init_db`` auto-create a missing DB, so for an absent named
    board they are inventory mutators wearing a reader's clothes — that is the
    hole a public lock over ``create_board`` alone leaves open.

    The predicate is deliberately narrow, so the hot path pays nothing and the
    fleet is not frozen by ordinary reads:

    * only ``<boards_root>/<slug>/kanban.db`` qualifies — the canonical shape of
      an inventory entry, derived from :func:`boards_root`, never guessed. The
      ``default`` board's legacy ``<root>/kanban.db`` and any path outside the
      boards root (tests, tooling passing an explicit ``db_path``) are not
      entries in that directory, so they are not gated;
    * a directory that already holds a board (``board.json`` or ``kanban.db``,
      i.e. exactly what :func:`list_boards` enumerates) is NOT new — connecting
      to an existing board changes no entry and stays lock-free.
    """
    from hermes_cli import kanban_db as _kb

    board_dir = db_path.parent
    if not path_is_board_entry(db_path):
        return False
    return not _kb._dir_holds_board(board_dir)


def _open_lock_file(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.open("a+b")


def _acquire_file_lock(handle, path: Path, budget: float, deadline: float) -> None:
    """Poll the kernel lock until taken, or raise on deadline.

    Polled rather than a blocking ``flock`` so the deadline is honoured on
    every platform (Windows ``msvcrt.locking`` has no interruptible blocking
    form that respects one) and so a wedged holder can never wedge the caller.
    """
    from hermes_cli.kanban_db_connect import _try_lock_nb

    while True:
        try:
            if _try_lock_nb(handle):
                return
        except OSError:
            pass
        if time.monotonic() >= deadline:
            raise BoardInventoryLockTimeout(path, budget)
        time.sleep(_POLL_SECONDS)


def _release_file_lock(handle) -> None:
    from hermes_cli.kanban_db_connect import _unlock

    with contextlib.suppress(OSError):
        _unlock(handle)
