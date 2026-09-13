"""Tests for the JS/TS ruleset in ``scripts/check-windows-footguns.py``.

Added alongside the JS/TS ruleset itself: the Python ruleset only ever
scanned hermes_cli/gateway/tools/etc — the entire Electron/TS desktop app
(apps/desktop/{src,electron,scripts}) was a blind spot the audit flagged as
a MEDIUM tooling gap. This file exercises each new JS/TS rule directly
(mirroring test_footgun_subprocess_encoding.py's approach for the Python
side) plus a regression-proof fixture: a small synthetic "pre-fix" source
tree containing each of the audit's JS-side findings, asserting the
checker actually catches them when run against unfixed code.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LINTER_PATH = REPO_ROOT / "scripts" / "check-windows-footguns.py"


def _load_linter_module():
    """Import the linter script as a module (it's not a package).

    Register the module in sys.modules BEFORE exec_module so that
    ``@dataclass`` can resolve ``cls.__module__`` via
    ``sys.modules.get(cls.__module__).__dict__`` (CPython 3.11+ dataclass
    internals require this).
    """
    spec = importlib.util.spec_from_file_location("check_windows_footguns_js", LINTER_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_windows_footguns_js"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def linter():
    return _load_linter_module()


def _find_footgun(linter, name: str):
    for fg in linter.JS_FOOTGUNS:
        if fg.name == name:
            return fg
    pytest.fail(f"JS footgun rule '{name}' not found in JS_FOOTGUNS")


def _scan_ts_source(linter, source: str, filename: str = "fixture.ts"):
    """Write `source` to a temp .ts file under a temp dir and run the full
    scan_file() path (docstring/comment-stripping, GUARD_HINTS, suppression
    marker, context_check — the whole pipeline), not just a single rule."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / filename
        path.write_text(source, encoding="utf-8")
        return linter.scan_file(path, linter.JS_FOOTGUNS)


# ---------------------------------------------------------------------------
# should_scan_file / ruleset_for_file wiring
# ---------------------------------------------------------------------------


class TestFileScoping:
    def test_ts_file_under_apps_desktop_src_is_in_scope(self, linter):
        path = linter.REPO_ROOT / "apps" / "desktop" / "src" / "lib" / "whatever.ts"
        assert linter.should_scan_file(path)

    def test_tsx_file_under_apps_desktop_electron_is_in_scope(self, linter):
        path = linter.REPO_ROOT / "apps" / "desktop" / "electron" / "whatever.tsx"
        assert linter.should_scan_file(path)

    def test_mjs_file_under_apps_desktop_scripts_is_in_scope(self, linter):
        path = linter.REPO_ROOT / "apps" / "desktop" / "scripts" / "whatever.mjs"
        assert linter.should_scan_file(path)

    def test_ts_file_outside_apps_desktop_is_out_of_scope(self, linter):
        # apps/shared and apps/bootstrap-installer are real .ts trees in
        # this repo that this checker deliberately does not own yet.
        path = linter.REPO_ROOT / "apps" / "shared" / "src" / "index.ts"
        assert not linter.should_scan_file(path)

    def test_ts_file_under_apps_desktop_dist_is_out_of_scope(self, linter):
        # Build output, not source — must not be scanned even though the
        # path contains 'apps/desktop'.
        path = linter.REPO_ROOT / "apps" / "desktop" / "dist" / "whatever.js"
        assert not linter.should_scan_file(path)

    def test_ruleset_for_file_routes_ts_to_js_footguns(self, linter):
        path = linter.REPO_ROOT / "apps" / "desktop" / "src" / "x.ts"
        assert linter.ruleset_for_file(path) is linter.JS_FOOTGUNS

    def test_ruleset_for_file_routes_py_to_footguns(self, linter):
        path = linter.REPO_ROOT / "scripts" / "x.py"
        assert linter.ruleset_for_file(path) is linter.FOOTGUNS


# ---------------------------------------------------------------------------
# fs.watch() without a nearby error handler
# ---------------------------------------------------------------------------


class TestFsWatch:
    def test_flags_fs_watch_with_no_error_handler(self, linter):
        source = "\n".join(
            [
                "function watchIt(dir) {",
                "  const watcher = fs.watch(dir, () => {",
                "    console.log('changed')",
                "  })",
                "  return watcher",
                "}",
            ]
        )
        matches = _scan_ts_source(linter, source)
        names = {fg.name for _, _, fg in matches}
        assert "fs.watch() without a nearby error handler" in names

    def test_does_not_flag_fs_watch_with_nearby_error_handler(self, linter):
        source = "\n".join(
            [
                "function watchIt(dir) {",
                "  const watcher = fs.watch(dir, () => {",
                "    console.log('changed')",
                "  })",
                "  watcher.on('error', (err) => {",
                "    watcher.close()",
                "  })",
                "  return watcher",
                "}",
            ]
        )
        matches = _scan_ts_source(linter, source)
        names = {fg.name for _, _, fg in matches}
        assert "fs.watch() without a nearby error handler" not in names

    def test_does_not_flag_fs_watch_guarded_by_helper(self, linter):
        source = "\n".join(
            [
                "const watcher = fs.watch(dir, cb)",
                "guardWatcherErrors(watcher, onErr)",
            ]
        )
        matches = _scan_ts_source(linter, source)
        names = {fg.name for _, _, fg in matches}
        assert "fs.watch() without a nearby error handler" not in names

    def test_does_not_flag_fs_watch_mentioned_only_in_a_comment(self, linter):
        # Regression case found while building this ruleset: a JS `//`
        # comment merely MENTIONING fs.watch() in prose must not be
        # scanned as if it were a live call — requires JS comment
        # stripping (_strip_code_js), not just the Python `#` stripper.
        source = "// main.ts owns the actual fs.watch() calls, IPC wiring\nconst x = 1"
        matches = _scan_ts_source(linter, source)
        names = {fg.name for _, _, fg in matches}
        assert "fs.watch() without a nearby error handler" not in names

    def test_suppression_marker_js_style(self, linter):
        source = "const watcher = fs.watch(dir, cb) // windows-footgun: ok — reviewed"
        matches = _scan_ts_source(linter, source)
        assert matches == []


# ---------------------------------------------------------------------------
# split('\n') on child process output
# ---------------------------------------------------------------------------


class TestSplitNewline:
    def test_flags_split_on_stdout(self, linter):
        source = "const line = stdout.trim().split('\\n').filter(Boolean).pop()"
        matches = _scan_ts_source(linter, source)
        names = {fg.name for _, _, fg in matches}
        assert "split('\\n') on child process output" in names

    def test_flags_split_on_stderr(self, linter):
        source = "const lines = result.stderr.split('\\n')"
        matches = _scan_ts_source(linter, source)
        names = {fg.name for _, _, fg in matches}
        assert "split('\\n') on child process output" in names

    def test_does_not_flag_split_on_unrelated_string(self, linter):
        source = "const parts = someRandomText.split('\\n')"
        matches = _scan_ts_source(linter, source)
        names = {fg.name for _, _, fg in matches}
        assert "split('\\n') on child process output" not in names


# ---------------------------------------------------------------------------
# process.env.HOME
# ---------------------------------------------------------------------------


class TestProcessEnvHome:
    def test_flags_process_env_home(self, linter):
        source = "const home = process.env.HOME"
        matches = _scan_ts_source(linter, source)
        names = {fg.name for _, _, fg in matches}
        assert "process.env.HOME" in names

    def test_does_not_flag_process_env_other_var(self, linter):
        source = "const p = process.env.PATH"
        matches = _scan_ts_source(linter, source)
        names = {fg.name for _, _, fg in matches}
        assert "process.env.HOME" not in names


# ---------------------------------------------------------------------------
# 'darwin' ternary lacking a win32 branch
# ---------------------------------------------------------------------------


class TestDarwinTernary:
    def test_flags_darwin_ternary_without_win32(self, linter):
        source = "const shell = platform === 'darwin' ? '/bin/zsh' : '/bin/bash'"
        matches = _scan_ts_source(linter, source)
        names = {fg.name for _, _, fg in matches}
        assert "'darwin' ternary lacking a win32 branch" in names

    def test_does_not_flag_when_win32_handled_nearby(self, linter):
        source = "\n".join(
            [
                "if (platform === 'win32') {",
                "  return winValue",
                "}",
                "",
                "const x = platform === 'darwin' ? macValue : linuxValue",
            ]
        )
        matches = _scan_ts_source(linter, source)
        names = {fg.name for _, _, fg in matches}
        assert "'darwin' ternary lacking a win32 branch" not in names

    def test_does_not_flag_accelerator_strings(self, linter):
        source = "accelerator: process.platform === 'darwin' ? 'Alt+Cmd+I' : 'Ctrl+Shift+I',"
        matches = _scan_ts_source(linter, source)
        names = {fg.name for _, _, fg in matches}
        assert "'darwin' ternary lacking a win32 branch" not in names


# ---------------------------------------------------------------------------
# template-literal filesystem path join with bare '/'
# ---------------------------------------------------------------------------


class TestTemplateLiteralPathJoin:
    def test_flags_dir_template_join(self, linter):
        source = "const p = `${dir}/${filename}`"
        matches = _scan_ts_source(linter, source)
        names = {fg.name for _, _, fg in matches}
        assert "template-literal filesystem path join with bare '/'" in names

    def test_flags_home_template_join(self, linter):
        source = "const opened = await window.hermesDesktop.openDir(`${home}/plugins`)"
        matches = _scan_ts_source(linter, source)
        names = {fg.name for _, _, fg in matches}
        assert "template-literal filesystem path join with bare '/'" in names

    def test_does_not_flag_url_template_join(self, linter):
        # `base`/`url` interpolation is a URL path join, not a filesystem
        # join — '/' is correct there on every OS.
        source = "await bareJsonGet(`${base}/api/sessions`)"
        matches = _scan_ts_source(linter, source)
        names = {fg.name for _, _, fg in matches}
        assert "template-literal filesystem path join with bare '/'" not in names

    def test_does_not_flag_pathname_startswith(self, linter):
        source = "return request.pathname.startsWith(`${basePath}/`)"
        matches = _scan_ts_source(linter, source)
        names = {fg.name for _, _, fg in matches}
        assert "template-literal filesystem path join with bare '/'" not in names

    def test_does_not_flag_test_files(self, linter):
        source = "const p = `${dir}/${filename}`"
        matches = _scan_ts_source(linter, source, filename="whatever.test.ts")
        names = {fg.name for _, _, fg in matches}
        assert "template-literal filesystem path join with bare '/'" not in names


# ---------------------------------------------------------------------------
# spawn('bash'|'sh') without a platform guard
# ---------------------------------------------------------------------------


class TestSpawnBash:
    def test_flags_spawn_bash(self, linter):
        source = "const child = spawn('bash', [scriptPath, ...args])"
        matches = _scan_ts_source(linter, source)
        names = {fg.name for _, _, fg in matches}
        assert "spawn('bash'|'sh') without a platform guard" in names

    def test_flags_spawn_sh(self, linter):
        source = "const child = spawn('sh', ['-c', cmd])"
        matches = _scan_ts_source(linter, source)
        names = {fg.name for _, _, fg in matches}
        assert "spawn('bash'|'sh') without a platform guard" in names

    def test_does_not_flag_guarded_spawn(self, linter):
        source = "\n".join(
            [
                "const isPosix = process.platform !== 'win32'",
                "const child = isPosix ? spawn('bash', args) : spawn('powershell', args)",
            ]
        )
        matches = _scan_ts_source(linter, source)
        names = {fg.name for _, _, fg in matches}
        assert "spawn('bash'|'sh') without a platform guard" not in names


# ---------------------------------------------------------------------------
# metaKey without a ctrlKey fallback
# ---------------------------------------------------------------------------


class TestMetaKey:
    def test_flags_metakey_alone(self, linter):
        source = "if (event.metaKey) { close() }"
        matches = _scan_ts_source(linter, source)
        names = {fg.name for _, _, fg in matches}
        assert "metaKey without a ctrlKey fallback" in names

    def test_does_not_flag_metakey_with_ctrlkey_same_line(self, linter):
        source = "if (event.metaKey || event.ctrlKey) { close() }"
        matches = _scan_ts_source(linter, source)
        names = {fg.name for _, _, fg in matches}
        assert "metaKey without a ctrlKey fallback" not in names

    def test_does_not_flag_metakey_with_ctrlkey_nearby_line(self, linter):
        source = "\n".join(
            [
                "if (",
                "  event.key === 'Backspace' &&",
                "  !event.metaKey &&",
                "  !event.ctrlKey &&",
                "  !event.altKey",
                ") {",
                "  doThing()",
                "}",
            ]
        )
        matches = _scan_ts_source(linter, source)
        names = {fg.name for _, _, fg in matches}
        assert "metaKey without a ctrlKey fallback" not in names

    def test_does_not_flag_test_files(self, linter):
        source = "expect(comboFromEvent(keydown({ metaKey: true }))).toBe('mod+k')"
        matches = _scan_ts_source(linter, source, filename="combo.test.ts")
        names = {fg.name for _, _, fg in matches}
        assert "metaKey without a ctrlKey fallback" not in names


# ---------------------------------------------------------------------------
# JS comment/string handling — shared plumbing the whole ruleset depends on
# ---------------------------------------------------------------------------


class TestJsCommentStripping:
    def test_find_unquoted_double_slash_ignores_url_in_string(self, linter):
        line = "const url = 'https://example.com/path'"
        assert linter._find_unquoted_double_slash(line) is None

    def test_find_unquoted_double_slash_finds_real_comment(self, linter):
        line = "const x = 1 // a comment"
        idx = linter._find_unquoted_double_slash(line)
        assert idx is not None
        assert line[:idx].strip() == "const x = 1"

    def test_strip_code_js_drops_whole_line_comment(self, linter):
        assert linter._strip_code_js("// just a comment") == ""

    def test_strip_code_js_keeps_code_before_trailing_comment(self, linter):
        result = linter._strip_code_js("const x = 1 // trailing")
        assert result.strip() == "const x = 1"

    def test_strip_code_js_does_not_break_on_template_literal_with_slashes(self, linter):
        # Template literals can contain '/' freely; only an actual //
        # sequence outside any string/template should be treated as a
        # comment start.
        line = "const p = `${dir}/${name}`"
        assert linter._strip_code_js(line) == line


# ---------------------------------------------------------------------------
# Regression-proof fixture: catches the audit's JS findings pre-fix.
#
# This mirrors the card's acceptance criterion "Checker catches the audit's
# JS findings when run against pre-fix code (regression-prove on a
# fixture)" — a small synthetic file with each finding UNFIXED, scanned with
# the real JS_FOOTGUNS ruleset end-to-end (not cherry-picked single rules).
# ---------------------------------------------------------------------------


PRE_FIX_FIXTURE = """
import { spawn } from 'node:child_process'
import fs from 'node:fs'

// fs.watch with no error handler — Windows EPERM on delete/rename crashes
// the whole main process.
function watchPreviewFile(dir) {
  const watcher = fs.watch(dir, (_eventType, filename) => {
    console.log('changed', filename)
  })
  return watcher
}

// CRLF-unsafe split on child process stdout.
function parseGitStatus(result) {
  return result.stdout.trim().split('\\n').filter(Boolean)
}

// process.env.HOME is unset on Windows.
function homeDir() {
  return process.env.HOME
}

// darwin ternary silently assumes Linux+Windows behave the same.
function shellFor(platform) {
  return platform === 'darwin' ? '/bin/zsh' : '/bin/bash'
}

// template-literal path join breaks on backslash-separated Windows paths.
function pluginsDir(home) {
  return `${home}/plugins`
}

// spawn('bash') assumes a POSIX shell is on PATH.
function runScript(scriptPath, args) {
  return spawn('bash', [scriptPath, ...args])
}

// metaKey-only shortcut has no Windows/Linux equivalent.
function isCloseClick(event) {
  return event.button === 0 && event.metaKey
}
"""


def test_fixture_regression_proves_all_seven_js_rules_fire_pre_fix(linter):
    """Regression-prove: every one of the 7 JS/TS rules fires against this
    small pre-fix fixture, exactly mirroring the audit's original findings
    (fs.watch, split('\\n'), process.env.HOME, darwin ternary, template
    path join, spawn('bash'), metaKey) — so a future edit that weakens any
    rule's regex/context_check to the point of missing its own textbook
    case is caught here, independent of the current tree's real findings."""
    matches = _scan_ts_source(linter, PRE_FIX_FIXTURE, filename="pre_fix_fixture.ts")
    fired_rule_names = {fg.name for _, _, fg in matches}

    expected_rule_names = {fg.name for fg in linter.JS_FOOTGUNS}
    missing = expected_rule_names - fired_rule_names

    assert not missing, (
        f"Rules that did NOT fire against their own textbook pre-fix "
        f"example: {sorted(missing)}\nAll matches: {matches}"
    )
