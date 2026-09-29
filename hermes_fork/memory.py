"""Configurable outer cap on external memory-provider prefetch (tier T1, see FORK.md).

Upstream ``MemoryManager`` bounds every external provider's ``prefetch()`` with
``thread.join(_EXTERNAL_PREFETCH_TIMEOUT_S)`` (8.0 s), and ``agent_init`` builds
the manager with no override, so there is no config knob. A provider whose own
wait is longer (hindsight's ``prefetch_join_timeout``, or a synchronous recall
that takes 9 s) is dropped for the turn by this outer cap before its own timeout
can fire: ``Memory provider '<name>' prefetch timed out after 8.0s``.

Config key (``config.yaml``)::

    memory:
      external_prefetch_timeout: 30   # seconds, > 0

* Unset (or ``null``): ``None`` is returned and upstream's 8.0 s default applies,
  so without the key behaviour is unchanged.
* Not a finite number > 0 (``0``, ``-1``, ``"abc"``, ``true``, ``nan``, ``inf``, an integer too large for a float):
  one warning per distinct value, then the upstream default.
* Set it >= the active provider's OWN wait (for hindsight, ``prefetch_join_timeout``
  in ``$HERMES_HOME/hindsight/config.json``, or its worst recall latency when
  ``recall_sync`` is on). The cap is the latest a turn waits for memory, so a
  larger value also means a slow provider can delay the first token that long.
  Nothing auto-widens it or warns on a mismatch: providers' join timeouts are
  private attributes, not a contract this module can read.

Read per agent construction from the same ``memory`` section ``agent_init``
already resolved, so a profile's own ``config.yaml`` governs its sessions.

Called from exactly one site: the ``memory-external-prefetch-timeout`` FORK ANCHOR
in ``agent.agent_init._init_memory``, where the external provider's
``MemoryManager`` is constructed.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Optional

logger = logging.getLogger(__name__)

CONFIG_KEY = "external_prefetch_timeout"
_warned: set[str] = set()


def external_prefetch_timeout(mem_config: Any) -> Optional[float]:
    """Seconds for ``MemoryManager(external_prefetch_timeout=...)``; None = upstream default."""
    raw = mem_config.get(CONFIG_KEY) if isinstance(mem_config, dict) else None
    if raw is None:
        return None
    value: Optional[float] = None
    if not isinstance(raw, bool):
        try:
            value = float(raw)
        except (TypeError, ValueError, OverflowError):  # OverflowError: int too large for a float
            value = None
    if value is not None and math.isfinite(value) and value > 0:
        return value
    if repr(raw) not in _warned:
        _warned.add(repr(raw))
        logger.warning(
            "Ignoring memory.%s=%r: expected a number of seconds > 0; using the default outer "
            "prefetch cap", CONFIG_KEY, raw,
        )
    return None
