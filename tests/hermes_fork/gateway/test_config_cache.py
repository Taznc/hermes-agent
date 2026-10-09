"""Config-cache publication must not serialize detached copies."""
from concurrent.futures import ThreadPoolExecutor
import copy
from pathlib import Path
import threading

import pytest


@pytest.fixture
def server(monkeypatch, tmp_path):
    from tui_gateway import server

    assert Path(server.__file__).resolve().parents[1] == Path(__file__).resolve().parents[3]
    monkeypatch.setattr(server, "_hermes_home", str(tmp_path))
    monkeypatch.setattr(server, "_cfg_cache", None)
    monkeypatch.setattr(server, "_cfg_sig", None)
    monkeypatch.setattr(server, "_cfg_path", None)
    monkeypatch.setattr(server, "_cfg_lock", threading.Lock())
    (tmp_path / "config.yaml").write_text("probe:\n  items: [original]\n")
    from hermes_fork.gateway.config_cache import install

    monkeypatch.setattr(server, "_load_cfg_raw", server._load_cfg_raw)
    monkeypatch.setattr(server, "_save_cfg", server._save_cfg)
    install(server)
    return server


@pytest.mark.parametrize("operation", ["hit", "miss", "save"])
def test_detached_copy_does_not_hold_publication_lock(server, monkeypatch, operation):
    if operation == "hit":
        server._load_cfg_raw()
    started, release = threading.Event(), threading.Event()
    deepcopy = copy.deepcopy

    def paused_copy(value, memo=None):
        if threading.current_thread().name.startswith("paused-copy"):
            started.set()
            assert release.wait(10), "copy release barrier timed out"
        return deepcopy(value, memo)

    monkeypatch.setattr(copy, "deepcopy", paused_copy)
    with ThreadPoolExecutor(1, thread_name_prefix="paused-copy") as executor:
        result = (executor.submit(server._save_cfg, {"probe": {"items": ["original"]}})
                  if operation == "save" else executor.submit(server._load_cfg_raw))
        try:
            assert started.wait(10), "reader did not reach copy barrier"
            acquired = server._cfg_lock.acquire(blocking=False)
            if acquired:
                server._cfg_lock.release()
            assert acquired, "detached cache-hit copy still holds publication lock"
        finally:
            release.set()
        returned = result.result()
        if operation != "save":
            assert returned["probe"]["items"] == ["original"]
        assert server._load_cfg_raw()["probe"]["items"] == ["original"]


def test_overlapping_savers_never_label_old_data_with_new_signature(server, monkeypatch):
    import hermes_cli.config as config

    written, release, second_copy = threading.Event(), threading.Event(), threading.Event()
    replace, deepcopy = config.atomic_config_replace, copy.deepcopy
    active_lock = threading.Lock()
    active, maximum = 0, 0

    def observed_copy(value, memo=None):
        if threading.current_thread().name.startswith("second-save"):
            second_copy.set()
        return deepcopy(value, memo)

    def paused_replace(path, cfg):
        nonlocal active, maximum
        with active_lock:
            active += 1
            maximum = max(maximum, active)
        try:
            replace(path, cfg)
            if threading.current_thread().name.startswith("first-save"):
                written.set()
                assert release.wait(10), "writer release barrier timed out"
        finally:
            with active_lock:
                active -= 1

    monkeypatch.setattr(config, "atomic_config_replace", paused_replace)
    monkeypatch.setattr(copy, "deepcopy", observed_copy)
    with ThreadPoolExecutor(1, thread_name_prefix="first-save") as first_pool, \
            ThreadPoolExecutor(1, thread_name_prefix="second-save") as second_pool:
        first = first_pool.submit(server._save_cfg, {"probe": "first"})
        try:
            assert written.wait(10)
            second = second_pool.submit(server._save_cfg, {"probe": "second"})
            assert second_copy.wait(10)
            # Saver I/O must not take the reader's publication lock.
            assert server._load_cfg_raw() == {"probe": "first"}
        finally:
            release.set()
        first.result()
        second.result()
    assert maximum == 1, "saver I/O overlapped and can mislabel cached payloads"
    assert server._load_cfg_raw() == config.read_user_config_raw(server._active_config_path())


def test_miss_copy_cannot_replace_a_newer_saver_snapshot(server, monkeypatch):
    started, release = threading.Event(), threading.Event()
    deepcopy = copy.deepcopy

    def paused_copy(value, memo=None):
        if threading.current_thread().name.startswith("miss-copy"):
            started.set()
            assert release.wait(10)
        return deepcopy(value, memo)

    monkeypatch.setattr(copy, "deepcopy", paused_copy)
    with ThreadPoolExecutor(1, thread_name_prefix="miss-copy") as pool:
        reader = pool.submit(server._load_cfg_raw)
        try:
            assert started.wait(10)
            server._save_cfg({"probe": "new"})
        finally:
            release.set()
        assert reader.result() == {"probe": {"items": ["original"]}}
    assert server._cfg_cache == {"probe": "new"}
    assert server._load_cfg_raw() == {"probe": "new"}


def test_raw_roundtrip_stays_detached_and_serializable(server):
    import json
    import hermes_cli.config as config

    first = server._load_cfg_raw()
    first["probe"]["items"].append("edit")
    assert server._load_cfg_raw()["probe"]["items"] == ["original"]
    server._save_cfg(cfg=first)  # real callers use both positional and keyword spelling
    first["probe"]["items"].append("not-saved")
    hit = server._load_cfg_raw()
    assert json.loads(json.dumps(hit)) == {"probe": {"items": ["original", "edit"]}}
    hit["probe"]["items"].clear()
    assert server._load_cfg_raw() == config.read_user_config_raw(server._active_config_path())


def test_raw_and_effective_reads_remain_separate(server, monkeypatch, tmp_path):
    path = server._active_config_path()
    path.write_text("custom_prompt: '${CACHE_TEST_VALUE}'\nagent:\n  reasoning_effort: low\n")
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "config.yaml").write_text("agent:\n  reasoning_effort: high\n")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    monkeypatch.setenv("CACHE_TEST_VALUE", "one")
    assert server._load_cfg()["custom_prompt"] == "one"
    assert server._load_cfg()["agent"]["reasoning_effort"] == "high"
    raw = server._load_cfg_raw()
    assert raw["custom_prompt"] == "${CACHE_TEST_VALUE}"
    assert raw["agent"]["reasoning_effort"] == "low"
    monkeypatch.setenv("CACHE_TEST_VALUE", "two")
    assert server._load_cfg()["custom_prompt"] == "two"
    assert server._load_cfg_raw() == raw
    server._save_cfg(raw)
    saved = path.read_text()
    assert "${CACHE_TEST_VALUE}" in saved and "high" not in saved
    assert "max_turns" not in raw


def test_missing_and_non_mapping_keep_legacy_empty_fallback(server):
    path = server._active_config_path()
    path.unlink()
    assert server._load_cfg_raw() == {}
    server._save_cfg({"created": True})
    assert server._load_cfg_raw() == {"created": True}
    path.write_text("- not-a-mapping\n")
    assert server._load_cfg_raw() == {}


def test_invalid_yaml_is_failed_read_and_refused_on_save(server):
    from hermes_cli.config_read_errors import FailedConfigRead

    path = server._active_config_path()
    path.write_text("probe: [unclosed\n")
    before = path.read_bytes()
    failed = server._load_cfg_raw()
    assert isinstance(failed, FailedConfigRead)
    assert failed == {}
    failed["edit"] = True
    with pytest.raises(RuntimeError, match="formatting error.*not saved"):
        server._save_cfg(failed)
    assert path.read_bytes() == before
    path.write_text("recovered: true\n")
    assert server._load_cfg_raw() == {"recovered": True}


@pytest.mark.parametrize("failure_site", ["read", "stat"])
def test_transient_emfile_does_not_poison_cache(server, monkeypatch, failure_site):
    import errno
    import builtins
    from hermes_cli.config_read_errors import FailedConfigRead

    path = server._active_config_path()
    before = path.read_bytes()
    with monkeypatch.context() as patch:
        if failure_site == "read":
            original = builtins.open
            def fail_open(file, *args, **kwargs):
                if Path(file) == path:
                    raise OSError(errno.EMFILE, "synthetic descriptor exhaustion")
                return original(file, *args, **kwargs)
            patch.setattr(builtins, "open", fail_open)
        else:
            original = Path.stat
            def fail_stat(self, *args, **kwargs):
                if self == path:
                    raise OSError(errno.EMFILE, "synthetic descriptor exhaustion")
                return original(self, *args, **kwargs)
            patch.setattr(Path, "stat", fail_stat)
        failed = server._load_cfg_raw()
    assert isinstance(failed, FailedConfigRead)
    assert isinstance(failed.read_error, OSError)
    assert failed.read_error.errno == errno.EMFILE
    with pytest.raises(RuntimeError, match="could not be read.*not saved"):
        server._save_cfg(failed)
    assert path.read_bytes() == before
    assert server._load_cfg_raw()["probe"]["items"] == ["original"]


def test_failed_sentinel_cache_hit_preserves_refusal(server, monkeypatch):
    import hermes_cli.config as config
    from hermes_cli.config_read_errors import FailedConfigRead

    failure = FailedConfigRead({"fallback": ["last-good"]}, error=ValueError("synthetic parse failure"))
    monkeypatch.setattr(config, "read_user_config_raw", lambda path: failure)
    assert isinstance(server._load_cfg_raw(), FailedConfigRead)
    hit = server._load_cfg_raw()
    assert isinstance(hit, FailedConfigRead)
    hit["fallback"].append("edit")
    assert server._cfg_cache["fallback"] == ["last-good"]
    with pytest.raises(RuntimeError, match="formatting error.*not saved"):
        server._save_cfg(hit)


def test_replaced_inode_invalidates_equal_mtime_and_size(server):
    import os

    path = server._active_config_path()
    path.write_text("model: aaaa\n")
    assert server._load_cfg_raw() == {"model": "aaaa"}
    old = path.stat()
    replacement = path.with_name("replacement.yaml")
    replacement.write_text("model: bbbb\n")
    os.utime(replacement, ns=(old.st_atime_ns, old.st_mtime_ns))
    assert replacement.stat().st_size == old.st_size
    assert replacement.stat().st_ino != old.st_ino
    replacement.replace(path)
    assert server._load_cfg_raw() == {"model": "bbbb"}
    assert server._cfg_sig == server.file_signature(path.stat())


def test_install_is_idempotent_and_preserves_call_signatures(server):
    import inspect
    from hermes_fork.gateway.config_cache import install

    reader, saver, effective, lock = server._load_cfg_raw, server._save_cfg, server._load_cfg, server._cfg_lock
    install(server)
    assert (server._load_cfg_raw, server._save_cfg, server._load_cfg, server._cfg_lock) == (reader, saver, effective, lock)
    assert inspect.signature(reader) == inspect.signature(reader.__wrapped__)
    assert inspect.signature(saver) == inspect.signature(saver.__wrapped__)


def test_concurrent_profile_readers_and_saver_keep_captured_path(server, tmp_path):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override

    homes = [tmp_path / "profile-a", tmp_path / "profile-b"]
    for home in homes:
        home.mkdir()
        (home / "config.yaml").write_text(f"owner: {home.name}\nitems: [original]\n")
    ready = threading.Barrier(2)

    def worker(home):
        token = set_hermes_home_override(home)
        try:
            ready.wait(timeout=10)
            for i in range(20):
                cfg = server._load_cfg_raw()
                assert cfg["owner"] == home.name
                if home == homes[0]:
                    cfg["items"] = [i]
                    server._save_cfg(cfg)
            return server._load_cfg_raw()
        finally:
            reset_hermes_home_override(token)

    with ThreadPoolExecutor(2) as pool:
        results = [future.result() for future in [pool.submit(worker, home) for home in homes]]
    assert results[0] == {"owner": "profile-a", "items": [19]}
    assert results[1] == {"owner": "profile-b", "items": ["original"]}
    assert server._active_config_path() == tmp_path / "config.yaml"


def test_real_writeback_caller_uses_installed_raw_reader_and_saver(server):
    server._write_config_key("probe.items", ["written-through-caller"])
    assert server._load_cfg_raw() == {"probe": {"items": ["written-through-caller"]}}


def test_saver_stat_failure_keeps_legacy_signature_fallback(server, monkeypatch):
    signature = server.file_signature
    with monkeypatch.context() as patch:
        patch.setattr(server, "file_signature", lambda stat: (_ for _ in ()).throw(OSError("synthetic stat failure")))
        server._save_cfg({"probe": "saved"})
        assert server._cfg_sig is None
    assert server._load_cfg_raw() == {"probe": "saved"}
    assert server._cfg_sig == signature(server._active_config_path().stat())


def test_failed_read_refusal_happens_before_copying_its_fallback(server):
    from hermes_cli.config_read_errors import FailedConfigRead

    class NoCopy:
        def __deepcopy__(self, memo):
            raise AssertionError("a failed-read fallback must never be copied for save")

    before = server._active_config_path().read_bytes()
    failed = FailedConfigRead({"fallback": NoCopy()}, error=ValueError("synthetic parse failure"))
    with pytest.raises(RuntimeError, match="not saved"):
        server._save_cfg(failed)
    assert server._active_config_path().read_bytes() == before
