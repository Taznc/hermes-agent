"""Resume surface: a resumed session keeps the source stored on its row.

``session.resume`` (``tui_gateway/methods_session.py`` ``_Resume.mint``) resolves the live record's source
from the client's ``source`` param, else the PROCESS env (``HERMES_DESKTOP``). A client that omits the param
(the Desktop session-tile delegate, the bot-group resumes) against a backend without that env (the
web-desktop backend unit, a URL/cloud backend) relabelled a Desktop chat ``tui`` and stripped its
client-surface toolsets (spawn_session chips -> "unsupported"). tools/AGENTS.md: surface is a property of
the SESSION, never of the process env. Upstream shares the bug.

Order: explicit param > stored row > env (a rowless resume). :func:`install` rebinds ``_Resume.mint`` from
the ``gateway-fork-methods`` anchor (``hermes_fork.gateway.registry._install_gateway_seams``), so no
upstream file carries a line for it.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


def resume_source(params: dict, found: dict | None) -> str:
    """The source a resumed live record runs under: explicit ``source`` param, else the stored row's, else env."""
    from tui_gateway.server import _resolve_session_source

    explicit = params.get("source") if isinstance(params, dict) else None
    explicit = explicit.strip() if isinstance(explicit, str) else ""
    stored: Any = found.get("source") if isinstance(found, dict) else None
    stored = stored.strip() if isinstance(stored, str) else ""
    return _resolve_session_source(explicit or stored or None)


def _wrap_mint(original: Callable) -> Callable:
    if getattr(original, "__fork_wrapped__", None) is not None:
        return original

    resolve = resume_source  # bound now: tests reload modules under patch.dict(sys.modules)

    def mint(self, *args, **kwargs) -> tuple:
        sid, _source, *rest = original(self, *args, **kwargs)
        return (sid, resolve(self.params, self.found), *rest)

    mint.__fork_wrapped__ = original
    mint.__doc__ = original.__doc__
    return mint


def install(server: Any = None) -> None:
    """Rebind ``_Resume.mint`` so its ``source`` honours the stored row (idempotent)."""
    from tui_gateway import methods_session
    from tui_gateway.contracts import registry as contracts

    resume = getattr(methods_session, "_Resume", None)
    original = getattr(resume, "mint", None)
    if not callable(original):
        # Upstream renamed/moved the resume mint (tests/hermes_fork/gateway/test_session_surface.py pins it).
        if contracts.STRICT:
            raise RuntimeError("tui_gateway.methods_session._Resume.mint is gone; update hermes_fork.gateway.session_surface")
        logger.warning("resume surface fix not installed: no _Resume.mint")
        return
    resume.mint = _wrap_mint(original)
