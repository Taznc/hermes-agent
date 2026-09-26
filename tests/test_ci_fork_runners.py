"""Fork CI must not queue forever on NousResearch-only paid runners.

F30 PORT (ledger). dev's tests/test_ci_fork_runners.py (1bf34bc4f1,
89d7c365aa) proved the pattern on a smaller, since-diverged set of
workflow files; `next`'s workflow tree has grown (docker.yml matrices,
desktop-bundled-release.yml, windows-bundle-sdk.yml, windows-venv-e2e.yml,
e2e-desktop-core.yml did not exist in dev's original set), so this
re-derives coverage from a live sweep of every workflow file rather than
naming just the four originally-touched files.
"""
from pathlib import Path

import pytest
from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
OWNER = "github.repository_owner == 'NousResearch'"


def _load(path: Path):
    yaml = YAML(typ="base")
    return yaml.load(path.read_text(encoding="utf-8"))


def _iter_strings(value):
    if isinstance(value, dict):
        for child in value.values():
            yield from _iter_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_strings(child)
    elif isinstance(value, str):
        yield value


@pytest.mark.parametrize(
    "filename,large,standard",
    [
        ("tests.yml", "ubuntu-latest-96-core", "ubuntu-latest"),
        ("tests-os.yml", "windows-latest-32-core", "windows-latest"),
        ("tests-os.yml", "windows-latest-32-arm-core", "windows-latest"),
        ("js-tests.yml", "ubuntu-latest-32-core", "ubuntu-latest"),
        ("rust-tests.yml", "ubuntu-latest-32-core", "ubuntu-latest"),
        ("nix.yml", "ubuntu-latest-32-core", "ubuntu-latest"),
        ("e2e-desktop.yml", "ubuntu-latest-32-core", "ubuntu-latest"),
        ("e2e-desktop-core.yml", "ubuntu-latest-32-core", "ubuntu-latest"),
        ("docker.yml", "ubuntu-latest-32-core", "ubuntu-latest"),
        ("docker.yml", "ubuntu-latest-32-arm-core", "ubuntu-24.04-arm"),
        ("windows-venv-e2e.yml", "windows-latest-32-core", "windows-latest"),
        ("pm-bundle.yml", "windows-latest-32-core", "windows-latest"),
        ("pm-bundle.yml", "windows-latest-32-arm-core", "windows-latest"),
        ("desktop-bundled-release.yml", "windows-latest-32-core", "windows-latest"),
        ("desktop-bundled-release.yml", "windows-latest-32-arm-core", "windows-latest"),
        ("desktop-bundled-release.yml", "ubuntu-latest-32-core", "ubuntu-latest"),
    ],
)
def test_large_runner_labels_have_a_standard_fork_fallback(filename, large, standard):
    document = _load(WORKFLOWS / filename)
    expected = "${{ " + OWNER + " && '" + large + "' || '" + standard + "' }}"
    runners = [value for value in _iter_strings(document) if large in value]
    assert runners, f"No runner covered in {filename} for {large}"
    assert all(value == expected for value in runners), (filename, large, runners)


@pytest.mark.parametrize(
    "filename,arch_expr_fragment",
    [
        ("install-e2e-windows-run.yml", "inputs.bundle-arch == 'arm64'"),
        ("desktop-bundle-smoke.yml", "inputs.arch == 'arm64'"),
    ],
)
def test_compound_arch_and_owner_gate_has_standard_fallback(filename, arch_expr_fragment):
    """Files that already branch on arch get the owner-gate wrapped AROUND
    the existing arch ternary, not a separate/duplicated expression, and
    still resolve to a standard (non-paid) label on a non-Nous owner."""
    text = (WORKFLOWS / filename).read_text()
    matches = [line for line in text.splitlines() if arch_expr_fragment in line and "runs-on" in line]
    assert matches, f"expected arch-gated runs-on line in {filename}"
    for line in matches:
        assert OWNER in line, line
        assert "windows-latest-32" in line
        assert line.strip().endswith("'windows-latest' }}"), line


def test_windows_bundle_sdk_matrix_array_has_fork_fallback():
    document = _load(WORKFLOWS / "windows-bundle-sdk.yml")
    runners = document["jobs"]["windows-bundle-tools"]["strategy"]["matrix"]["runner"]
    expected = "${{ " + OWNER + " && 'windows-latest-32-arm-core' || 'windows-latest' }}"
    assert runners == [expected, expected]


def test_ci_yaml_baselines_push_on_next():
    document = _load(WORKFLOWS / "ci.yaml")
    assert document["on"]["push"]["branches"] == ["main", "next"]


def test_no_other_workflow_uses_an_unconditional_paid_runner():
    import re

    for path in WORKFLOWS.glob("*.yml"):
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if re.match(r"\s*(runs-on|runner):.*-\d+-(?:arm-)?core", line):
                assert OWNER in line, f"Unconditional paid runner: {path.name}: {line}"
