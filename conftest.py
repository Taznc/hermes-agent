"""Repo-root pytest hooks owned by the fork (T1: upstream ships no root conftest.py).

Keep this file tiny. Upstream test fixtures live in ``tests/conftest.py``; only
fork seams that must reach upstream's own suites belong here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_ROUTE_POLICY_TESTS = (
    Path(__file__).resolve().parent / "tests" / "hermes_fork" / "kanban" / "test_route_policy.py"
)


@pytest.fixture(autouse=True)
def _fork_route_policy_admits_other_suites(request, monkeypatch):
    """Unattended route policy (``hermes_fork/kanban/route_policy.py``) stays
    live only for its own tests; every other dispatch suite (upstream's and
    the fork's other kanban seams) uses model-less throwaway profiles to test
    claim/spawn mechanics, so admission is waived for them via the module's
    test-only seam."""
    if Path(str(request.node.path)).resolve() == _ROUTE_POLICY_TESTS:
        yield
        return
    try:
        from hermes_fork.kanban import route_policy
    except ImportError:  # pragma: no cover - fork package absent
        yield
        return
    monkeypatch.setattr(route_policy, "_ADMIT_ALL_FOR_TESTS", True)
    yield
