"""Plugin-contributed nested ``hermes kanban <action>`` CLI registration.

Every test drives the REAL discovery path in a subprocess: a fixture plugin is
written into a temp ``HERMES_HOME/plugins/``, opted in through
``plugins.enabled``, and reached by running ``python -m hermes_cli.main`` with
the argv a user would type. Nothing here monkeypatches the plugin manager, so a
green run means the argparse tree, discovery gate, and dispatch all agree.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


REPO_ROOT = str(Path(__file__).resolve().parents[2])

PLUGIN_ID = "nested-cli-fixture"

# ``register()`` appends to the witness file, so its presence proves plugin
# discovery ran for that invocation (and its absence proves it did not).
_PLUGIN_SOURCE = '''\
"""Fixture plugin: contributes one nested ``hermes kanban`` action."""

import json
import os
from pathlib import Path


def _witness():
    Path(os.environ["NESTED_CLI_WITNESS"]).write_text("registered\\n", encoding="utf-8")


def _setup(parser):
    parser.add_argument("--flavour", default="plain")
    parser.add_argument("--board", default=None)


def _handler(args):
    print("NESTED_OK " + json.dumps({
        "flavour": args.flavour,
        "board": args.board,
    }))
    return 0


def _top_level_setup(parser):
    parser.add_argument("--shout", action="store_true")


def _top_level_handler(args):
    print("TOPLEVEL_OK " + json.dumps({"shout": bool(args.shout)}))
    return 0


def register(ctx):
    _witness()
    ctx.register_cli_command(
        name="fixture-action",
        help="Fixture nested kanban action",
        setup_fn=_setup,
        handler_fn=_handler,
        description="Nested action contributed by a standalone plugin.",
        parent="kanban",
    )
    ctx.register_cli_command(
        name="fixture-toplevel",
        help="Fixture top-level command",
        setup_fn=_top_level_setup,
        handler_fn=_top_level_handler,
        description="Top-level command contributed by a standalone plugin.",
    )
'''


@pytest.fixture
def plugin_home(tmp_path: Path) -> Path:
    """A temp HERMES_HOME with the fixture plugin installed and enabled."""
    home = tmp_path / "hermes-home"
    plugin_dir = home / "plugins" / PLUGIN_ID
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "__init__.py").write_text(_PLUGIN_SOURCE, encoding="utf-8")
    (plugin_dir / "plugin.yaml").write_text(
        yaml.safe_dump({"name": PLUGIN_ID, "version": "0.1.0",
                        "description": "Nested CLI registration fixture"}),
        encoding="utf-8",
    )
    (home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": [PLUGIN_ID]}}), encoding="utf-8"
    )
    (tmp_path / "bundled-plugins").mkdir()
    (tmp_path / "os-home").mkdir()
    return home


def _subprocess_env(plugin_home: Path) -> dict:
    tmp_path = plugin_home.parent
    env = dict(os.environ)
    env.update(
        HOME=str(tmp_path / "os-home"),
        HERMES_HOME=str(plugin_home),
        HERMES_BUNDLED_PLUGINS=str(tmp_path / "bundled-plugins"),
        NESTED_CLI_WITNESS=str(tmp_path / "witness.txt"),
        PYTHONPATH=REPO_ROOT + os.pathsep + env.get("PYTHONPATH", ""),
    )
    return env


def _run(plugin_home: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", *argv],
        cwd=REPO_ROOT, env=_subprocess_env(plugin_home),
        capture_output=True, text=True, timeout=180,
    )


def _run_script(plugin_home: Path, script: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script)],
        cwd=REPO_ROOT, env=_subprocess_env(plugin_home),
        capture_output=True, text=True, timeout=180,
    )


def _payload(marker: str, stdout: str) -> dict:
    line = next(ln for ln in stdout.splitlines() if ln.startswith(marker + " "))
    return json.loads(line[len(marker) + 1:])


def test_nested_plugin_action_runs_with_its_own_options(plugin_home: Path):
    """``hermes kanban <plugin-action>`` reaches the plugin handler (AC1)."""
    result = _run(plugin_home, "kanban", "fixture-action", "--flavour", "spicy")

    combined = result.stdout + result.stderr
    assert "NESTED_OK" in result.stdout, combined
    assert result.returncode == 0, combined
    assert _payload("NESTED_OK", result.stdout) == {"flavour": "spicy", "board": None}


def test_nested_plugin_action_cannot_override_builtin_kanban_action(plugin_home: Path):
    """A plugin naming a built-in kanban action is refused; the built-in still runs (AC3).

    The fixture attempts the built-in action ``list``. Fail-closed means argparse
    routes ``hermes kanban list`` to the shipped handler — the plugin's marker must
    be absent from a successful listing, not merely 'somewhere else in the output'.
    """
    collide = plugin_home / "plugins" / PLUGIN_ID / "__init__.py"
    collide.write_text(
        _PLUGIN_SOURCE.replace('name="fixture-action"', 'name="list"'), encoding="utf-8"
    )

    result = _run(plugin_home, "kanban", "list")

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "NESTED_OK" not in combined, combined
    # The built-in ran: its own empty-board listing, not an argparse/plugin error.
    assert "no matching tasks" in combined.lower(), combined


# The driver builds the REAL parser under an argv that forces plugin discovery, so
# the collision guard in ``_attach_nested_plugin_cli_command`` actually executes —
# the end-to-end ``hermes kanban list`` above never reaches it, because a built-in
# action name is exactly the case the discovery gate skips.
_COLLISION_DRIVER = '''\
import json, sys
sys.argv = ["hermes", "kanban", "fixture-action"]
from hermes_cli.main import _build_cli_parser, cmd_kanban

parser, _ = _build_cli_parser()
args = parser.parse_args(["kanban", "list"])
plugin_action = parser.parse_args(["kanban", "fixture-action"])
print("DRIVER " + json.dumps({
    "list_is_builtin": args.func is cmd_kanban,
    "list_action": getattr(args, "kanban_action", None),
    "plugin_action_attached": plugin_action.func.__module__.endswith(
        "nested-cli-fixture") or plugin_action.func.__name__ == "_handler",
}))
'''


def test_collision_guard_runs_when_discovery_is_forced(plugin_home: Path, tmp_path: Path):
    """The guard itself fail-closes while a sibling non-colliding action attaches (AC3).

    A plugin registering BOTH ``list`` (built-in) and ``fixture-action`` must end up
    with only the latter attached: ``hermes kanban list`` still routes to the built-in
    ``cmd_kanban`` dispatcher, not the plugin handler.
    """
    source = _PLUGIN_SOURCE.replace(
        'ctx.register_cli_command(\n        name="fixture-action"',
        'ctx.register_cli_command(\n        name="list",\n'
        '        help="Attempted built-in override",\n'
        '        setup_fn=_setup,\n'
        '        handler_fn=_handler,\n'
        '        parent="kanban",\n'
        '    )\n'
        '    ctx.register_cli_command(\n        name="fixture-action"',
    )
    (plugin_home / "plugins" / PLUGIN_ID / "__init__.py").write_text(source, encoding="utf-8")
    driver = tmp_path / "collision_driver.py"
    driver.write_text(_COLLISION_DRIVER, encoding="utf-8")

    result = _run_script(plugin_home, driver)

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    payload = _payload("DRIVER", result.stdout)
    assert payload["list_is_builtin"] is True, combined
    assert payload["list_action"] == "list", combined
    # Guard is scoped to the collision, not a blanket refusal of the plugin.
    assert payload["plugin_action_attached"] is True, combined


def test_top_level_plugin_command_still_parses_and_dispatches(plugin_home: Path):
    """A parent-less registration keeps its ``hermes <name>`` behavior (AC2)."""
    result = _run(plugin_home, "fixture-toplevel", "--shout")

    combined = result.stdout + result.stderr
    assert "TOPLEVEL_OK" in result.stdout, combined
    assert result.returncode == 0, combined
    assert _payload("TOPLEVEL_OK", result.stdout) == {"shout": True}


def test_nested_plugin_action_help_is_a_supported_argv_shape(plugin_home: Path):
    """``hermes kanban <plugin-action> --help`` prints the plugin's own help (AC4)."""
    result = _run(plugin_home, "kanban", "fixture-action", "--help")

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "--flavour" in result.stdout, combined
    assert "hermes kanban fixture-action" in result.stdout, combined


def test_builtin_kanban_action_does_not_trigger_plugin_discovery(plugin_home: Path):
    """Lazy discovery survives for built-in kanban actions.

    The fixture's ``register()`` writes a witness file, so its absence proves no
    plugin was imported while ``hermes kanban list`` ran — and its presence after
    the plugin-action invocation proves the witness itself works.
    """
    witness = plugin_home.parent / "witness.txt"

    builtin = _run(plugin_home, "kanban", "list")
    assert builtin.returncode == 0, builtin.stdout + builtin.stderr
    assert not witness.exists(), "plugin discovery ran for a built-in kanban action"

    plugin = _run(plugin_home, "kanban", "fixture-action")
    assert plugin.returncode == 0, plugin.stdout + plugin.stderr
    assert witness.exists(), "plugin discovery did not run for a plugin kanban action"


def test_board_flag_value_is_not_read_as_the_kanban_action(plugin_home: Path):
    """``--board <slug>`` must not make the gate mistake the slug for the action.

    The parent's value-taking flags are derived from the kanban parser itself, so
    ``hermes kanban --board <slug> list`` stays on the cheap built-in path.
    """
    witness = plugin_home.parent / "witness.txt"

    result = _run(plugin_home, "kanban", "--board", "default", "list")

    assert result.returncode == 0, result.stdout + result.stderr
    assert not witness.exists(), "a --board value was misread as a plugin action"


def test_unsupported_parent_is_refused(plugin_home: Path, tmp_path: Path):
    """A parent outside NESTED_CLI_PARENTS registers nothing and returns None."""
    driver = tmp_path / "unsupported_parent_driver.py"
    driver.write_text(
        "import json\n"
        "from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest\n"
        "mgr = PluginManager()\n"
        "ctx = PluginContext(PluginManifest(name='p'), mgr)\n"
        "bad = ctx.register_cli_command('act', 'h', lambda p: None, lambda a: 0, parent='cron')\n"
        "no_handler = ctx.register_cli_command('act2', 'h', lambda p: None, parent='kanban')\n"
        "ok = ctx.register_cli_command('act3', 'h', lambda p: None, lambda a: 0, parent='kanban')\n"
        "print('DRIVER ' + json.dumps({\n"
        "    'bad': bad is None, 'no_handler': no_handler is None, 'ok': ok is not None,\n"
        "    'keys': sorted(mgr._cli_commands),\n"
        "}))\n",
        encoding="utf-8",
    )

    result = _run_script(plugin_home, driver)

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    payload = _payload("DRIVER", result.stdout)
    assert payload["bad"] is True, combined
    assert payload["no_handler"] is True, combined
    assert payload["ok"] is True, combined
    assert payload["keys"] == ["kanban act3"], combined
