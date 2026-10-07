"""The ``session-compaction-watermark`` anchor on a real ``ContextCompressor`` (spec t_07c75c42 §3).

R1: without the anchor in ``ContextCompressor.threshold_tokens`` the "record raises the getter" assertions
fail with the actual value equal to the base trigger.
"""

from __future__ import annotations

from hermes_fork.compaction import watermark as wm

from .conftest import make_compressor, record_for

BASE = 484_000  # 1M window, 32K output reservation, 0.50


def test_no_record_returns_the_stored_value_unchanged():
    cc = make_compressor()
    assert cc.threshold_tokens == BASE == cc._threshold_tokens
    assert cc.should_compress(BASE) is True
    assert cc.should_compress(BASE - 1) is False


def test_record_for_the_bound_session_raises_every_reader():
    cc = make_compressor()
    cc._session_id = "S1"
    wm.install_watermark(cc, record_for(cc, 586_800))
    assert cc.threshold_tokens == 586_800
    assert cc.should_compress(586_799) is False
    assert cc.should_compress(586_800) is True
    # The stored (upstream) value is untouched by the anchor.
    assert cc._threshold_tokens == BASE


def test_record_tokens_are_clamped_to_the_ceiling():
    cc = make_compressor()
    wm.install_watermark(cc, record_for(cc, 999_999))
    assert cc.threshold_tokens == 871_200


def test_another_session_id_makes_the_record_dormant():
    cc = make_compressor()
    cc._session_id = "S1"
    wm.install_watermark(cc, record_for(cc, 586_800))
    assert cc.threshold_tokens == 586_800
    cc._session_id = "S2"  # e.g. /new, /branch, /reset rebinding
    assert cc.threshold_tokens == BASE
    assert vars(cc)[wm._SLOT] is not None  # dormant, not deleted
    cc._session_id = "S1"
    assert cc.threshold_tokens == 586_800


def test_another_window_makes_the_record_dormant_and_switching_back_reactivates():
    cc = make_compressor()
    wm.install_watermark(cc, record_for(cc, 586_800))
    cc.context_length = 200_000
    assert cc.threshold_tokens == 126_000
    cc.context_length = 1_000_000
    assert cc.threshold_tokens == 586_800


def test_cleared_watermark_does_not_leak_a_cap_between_base_and_watermark():
    """R3: a configured cap above the base, written into the stored value while the record was active, must
    not survive ``clear_watermark``."""
    cc = make_compressor(threshold_tokens_cap=550_000)
    assert cc.threshold_tokens == BASE  # cap is above the base: upstream ignores it
    wm.install_watermark(cc, record_for(cc, 677_600))
    # An upstream recompute while active (e.g. update_model / hot reload) compares the cap with the raised
    # getter value and writes the cap into the stored value.
    cc._apply_threshold_tokens_cap()
    assert cc._threshold_tokens == 550_000
    assert cc.threshold_tokens == 677_600
    wm.clear_watermark(cc)
    assert cc.threshold_tokens == BASE


def test_replacing_an_active_record_does_not_leak_its_cap():
    """A rollback to a lower prior record (defer lost the race) must not keep the cap the newer record let
    upstream write into the stored value."""
    cc = make_compressor(threshold_tokens_cap=550_000)
    wm.install_watermark(cc, record_for(cc, 500_000))
    assert cc.threshold_tokens == 500_000
    previous = wm.install_watermark(cc, record_for(cc, 677_600))
    cc._apply_threshold_tokens_cap()
    assert cc._threshold_tokens == 550_000
    wm.install_watermark(cc, previous)
    assert cc.threshold_tokens == 500_000


def test_session_rebind_does_not_leak_a_cap_written_while_active():
    cc = make_compressor(threshold_tokens_cap=550_000)
    cc._session_id = "S1"
    wm.install_watermark(cc, record_for(cc, 677_600))
    cc._apply_threshold_tokens_cap()
    assert cc._threshold_tokens == 550_000
    cc._session_id = "S2"
    assert cc.threshold_tokens == BASE


def test_validate_record_rejects_garbage_with_a_reason():
    for raw, needle in (
        ("586800", "JSON object"), ({"v": 2, "tokens": 1, "context_length": 1}, "version"),
        ({"v": 1, "tokens": -5, "context_length": 1}, "tokens"), ({"v": 1, "tokens": True, "context_length": 1}, "tokens"),
        ({"v": 1, "tokens": 5, "context_length": "1M"}, "context_length"),
    ):
        record, reason = wm.validate_record(raw)
        assert record is None and reason is not None and needle in reason
    record, reason = wm.validate_record({"v": 1, "tokens": 5, "context_length": 9, "ceiling_tokens": "x"})
    assert reason is None and record is not None and record["ceiling_tokens"] is None


def test_watermark_status_reports_active_and_base():
    cc = make_compressor()
    idle = wm.watermark_status(cc)
    assert idle["active"] is False and idle["threshold_tokens"] == BASE and idle["watermark_tokens"] is None
    cc.last_prompt_tokens = 490_000
    plan = wm.plan_raise(cc)
    wm.install_watermark(cc, plan.record)
    status = wm.watermark_status(cc)
    assert status == {"active": True, "watermark_tokens": 586_800, "threshold_tokens": 586_800,
                      "base_threshold_tokens": BASE, "ceiling_tokens": 871_200, "context_length": 1_000_000}
