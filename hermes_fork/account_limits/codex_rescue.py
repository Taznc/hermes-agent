"""Fork tier-4 Codex usage-credential rescue.

Extracted from ``agent/account_usage.py``'s ``_resolve_codex_usage_credentials``
behind a ``# >>> FORK ANCHOR: codex-usage-quota-rescue <<<`` call site.

``resolve_codex_runtime_credentials`` withholds a credential once the Codex
MODEL quota is spent while stating the credentials remain valid. The usage
endpoint is a read-only GET that consumes no model quota, so honoring that
refusal would black out the usage view exactly when the user needs it (to see
when the limit resets). Fall back to the stored access token, ONLY for
rate-limiting: a missing/invalid-credential AuthError must still fail so a
genuine re-login isn't masked by a stale token.
"""

from __future__ import annotations

import logging
from typing import Optional

from hermes_cli.auth import (
    AuthError,
    _codex_access_token_is_expiring,
    _import_codex_cli_tokens,
    _read_codex_tokens,
    is_rate_limited_auth_error,
)

logger = logging.getLogger("agent.account_usage")


def rescue_codex_usage_credentials(
    resolver_error: AuthError, base_url: Optional[str],
) -> tuple[str, str, Optional[str]]:
    """Last-resort ``(token, base_url, account_id)`` after the runtime resolver refused
    (``resolver_error``) AND the credential pool came up empty.

    Only a rate-limited refusal is rescued (via a still-valid stored access token);
    every other case keeps the pre-fork failure modes: the resolver's own error for
    quota exhaustion with no usable token, else the generic pool RuntimeError.
    """
    if is_rate_limited_auth_error(resolver_error):
        for reader in (_read_codex_tokens, _import_codex_cli_tokens):
            try:
                token_data = reader() or {}
            except AuthError:
                logger.debug("codex ▸ /usage stored-token rescue: %s found nothing", reader.__name__, exc_info=True)
                continue
            raw_tokens = token_data.get("tokens")
            tokens = raw_tokens if isinstance(raw_tokens, dict) else token_data
            stored_token = str(tokens.get("access_token", "") or "").strip()
            # An EXPIRED token would just 401. Refreshing here is not an option: Codex OAuth
            # refresh_tokens are single-use (see hermes_cli/auth.py), so spending one on a read-only
            # probe could rotate the user's real login out from under the Codex CLI.
            if stored_token and not _codex_access_token_is_expiring(stored_token, 0):
                account_id = str(tokens.get("account_id", "") or "").strip() or None
                logger.debug("codex ▸ /usage using stored token while model quota is exhausted")
                return stored_token, str(base_url or "").strip(), account_id
        # Surface the provider's own reason ("quota exhausted; retry after Ns") instead of a generic
        # pool error, so the UI says what is actually wrong rather than "no account configured".
        raise resolver_error
    raise RuntimeError("No available openai-codex credential in credential pool")
