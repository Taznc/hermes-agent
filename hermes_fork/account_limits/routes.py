"""Fork ``/api/account-limits`` dashboard route.

Extracted from ``hermes_cli/web_routers/analytics.py`` behind the
``# >>> FORK ANCHOR: account-limits-route <<<`` registration line in
``hermes_cli/web_server.py``. Credential resolution and upstream provider I/O
stay server-side; the response is explicitly allowlisted by
``serialize_account_usage``.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, HTTPException

from hermes_cli.web_deps import late

router = APIRouter()

# Late-bound so a test's monkeypatch on the owning module wins at call time.
_config_profile_scope = late("_config_profile_scope", "hermes_cli.web_server_profiles")


@router.get("/api/account-limits")
async def get_account_limits(
    providers: Optional[str] = None,
    profile: Optional[str] = None,
):
    """Sanitized live Claude/Codex account limits for first-party clients.

    Credential resolution and upstream provider I/O stay server-side; the
    response is explicitly allowlisted by ``serialize_account_usage``.  The
    profile scope is a ContextVar copied into the worker by ``to_thread``.
    """

    requested = None
    if providers is not None:
        requested = tuple(part.strip().lower() for part in providers.split(",") if part.strip())
        if not requested:
            raise HTTPException(status_code=422, detail="providers must name at least one supported provider")
    try:
        from agent.account_usage import fetch_account_limits, serialize_account_usage

        with _config_profile_scope(profile):
            snapshots = await asyncio.to_thread(fetch_account_limits, requested)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"accounts": [serialize_account_usage(snapshot) for snapshot in snapshots]}
