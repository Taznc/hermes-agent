"""Upstream tests that assert the opposite of a fork policy are marked strict-xfail. They are not deleted.

Each entry names a policy, the fork-owned test that pins the replacement behaviour, and the upstream
test node ids it supersedes. ``strict=True`` matters: if the fork policy is reverted (for example by a
sync that drops an anchor), the upstream test passes again and the strict xfail reports that as a
failure. Upstream test bodies stay byte-identical, so syncs merge cleanly.

``tests/conftest.py`` imports :func:`pytest_itemcollected` at the ``superseded-upstream-tests`` FORK
ANCHOR. The name is a pytest hook that upstream's conftest does not define, so the import registers it.
"""

from __future__ import annotations



_LIFECYCLE = ("t_3c25b726 lifecycle consent (hermes_fork/lifecycle_consent.py): reboot/shutdown ASK instead "
              "of hardline-deny, and lifecycle commands are never auto-approved by any mode. Replacement "
              "assertions: tests/hermes_fork/test_lifecycle_consent.py")

# node id (relative to the repo root) -> reason
SUPERSEDED: dict[str, str] = {
    "tests/hermes_cli/test_approvals_test.py::TestVerdicts::test_hardline_command_denies_with_rule_name": _LIFECYCLE,
    "tests/hermes_cli/test_approvals_test.py::TestVerdicts::test_mode_off_bypasses_dangerous_but_not_hardline":
        _LIFECYCLE,
    "tests/hermes_cli/test_approvals_test.py::TestOutputAndWiring::test_json_output_is_machine_readable": _LIFECYCLE,
    "tests/tools/test_hardline_blocklist.py::test_yolo_env_var_cannot_bypass_hardline": _LIFECYCLE,
    "tests/tools/test_approval.py::TestWebhookApprovalExclusion::test_webhook_dangerous_command_approves_when_opted_in":
        _LIFECYCLE,
}


def _relative_nodeid(item) -> str:
    """``item.nodeid`` is relative to the rootdir, so it already matches the keys when pytest runs from
    the repo root. Rebuild it from the file path anyway, so invocation from another directory also works."""
    try:
        rel = item.path.relative_to(item.config.rootpath).as_posix()
    except ValueError:
        return item.nodeid
    _, sep, rest = item.nodeid.partition("::")
    return f"{rel}{sep}{rest}"


def pytest_itemcollected(item) -> None:
    reason = SUPERSEDED.get(_relative_nodeid(item))
    if reason is not None:
        import pytest  # test-only dependency; never imported at runtime
        item.add_marker(pytest.mark.xfail(strict=True, reason=reason))
