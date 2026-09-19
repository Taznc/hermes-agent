"""Fork CI must not queue forever on NousResearch-only paid runners."""
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
OWNER = "github.repository_owner == 'NousResearch'"


@pytest.mark.parametrize(
    "filename,large,standard",
    [
        ("tests.yml", "ubuntu-latest-96-core", "ubuntu-latest"),
        ("tests-os.yml", "windows-latest-32-core", "windows-latest"),
        ("js-tests.yml", "ubuntu-latest-32-core", "ubuntu-latest"),
        ("rust-tests.yml", "ubuntu-latest-32-core", "ubuntu-latest"),
        ("e2e-desktop.yml", "ubuntu-latest-32-core", "ubuntu-latest"),
        ("nix.yml", "ubuntu-latest-32-core", "ubuntu-latest"),
        ("docker.yml", "ubuntu-latest-32-core", "ubuntu-latest"),
        ("docker.yml", "ubuntu-latest-32-arm-core", "ubuntu-24.04-arm"),
    ],
)
def test_large_runner_labels_have_a_standard_fork_fallback(filename, large, standard):
    document = yaml.safe_load((WORKFLOWS / filename).read_text())
    expected = "${{ " + OWNER + " && '" + large + "' || '" + standard + "' }}"
    def values(value):
        if isinstance(value, dict):
            for child in value.values():
                yield from values(child)
        elif isinstance(value, list):
            for child in value:
                yield from values(child)
        elif isinstance(value, str):
            yield value
    runners = [value for value in values(document) if large in value]
    assert runners, f"No runner covered in {filename}"
    assert all(value == expected for value in runners)


def test_fork_python_parallelism_fits_standard_runner():
    workflow = yaml.safe_load((WORKFLOWS / "tests.yml").read_text())
    step = next(step for step in workflow["jobs"]["test"]["steps"] if step.get("name") == "Run tests")
    assert step["env"]["HERMES_TEST_WORKERS"] == "${{ " + OWNER + " && 96 || 2 }}"
    # This is a runner-availability fix, not permission to skip or narrow tests.
    assert "scripts/run_tests.sh" in step["run"]
    assert "--files" not in step["run"]


def test_no_other_workflow_uses_an_unconditional_paid_runner():
    import re
    for path in WORKFLOWS.glob("*.yml"):
        for line in path.read_text().splitlines():
            if re.match(r"\s*(runs-on|runner):.*-\d+-(?:arm-)?core", line):
                assert OWNER in line, f"Unconditional paid runner: {path.name}: {line}"
