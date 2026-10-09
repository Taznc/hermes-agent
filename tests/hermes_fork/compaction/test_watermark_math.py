"""``plan_raise`` against the spec t_07c75c42 §1.2 table (pure planning, no install)."""

from __future__ import annotations

from hermes_fork.compaction import watermark as wm

from .conftest import make_compressor, record_for


def test_1m_window_first_cancel_matches_the_table():
    cc = make_compressor(1_000_000)
    assert cc.threshold_tokens == 484_000
    cc.last_prompt_tokens = 490_000
    plan = wm.plan_raise(cc)
    assert (plan.usable_tokens, plan.ceiling_tokens, plan.previous_threshold_tokens) == (968_000, 871_200, 484_000)
    assert plan.record is not None
    assert plan.record["tokens"] == 586_800
    assert plan.record["context_length"] == 1_000_000
    assert plan.record["base_threshold_tokens"] == 484_000
    assert plan.record["ceiling_tokens"] == 871_200


def test_1m_window_steps_up_to_the_ceiling_then_refuses():
    cc = make_compressor(1_000_000)
    cc.last_prompt_tokens = 0
    seen = []
    for _ in range(6):
        plan = wm.plan_raise(cc)
        if plan.record is None:
            seen.append(None)
            break
        seen.append(plan.record["tokens"])
        wm.install_watermark(cc, plan.record)
    assert seen == [580_800, 677_600, 774_400, 871_200, None]
    # Base survives every step: the record keeps the first base, not the previous watermark.
    assert wm.active_record(cc)["base_threshold_tokens"] == 484_000


def test_200k_small_ctx_floor_window():
    cc = make_compressor(200_000)
    assert cc.threshold_tokens == 126_000  # 0.75 small-ctx floor of 168K usable
    cc.last_prompt_tokens = 127_000
    plan = wm.plan_raise(cc)
    assert plan.record is not None and plan.record["tokens"] == 143_800
    wm.install_watermark(cc, plan.record)
    # 143,800 + 16,800 -> capped 151,200: 7,400 of headroom < 8,400 -> refused.
    refused = wm.plan_raise(cc)
    assert refused.record is None
    assert refused.ceiling_tokens == 151_200


def test_aux_summariser_ceiling_caps_the_raise():
    cc = make_compressor(1_000_000)
    cc._aux_context_ceiling = 600_000
    cc._threshold_tokens = None  # upstream re-derives with the aux ceiling (484K stays below it)
    cc.last_prompt_tokens = 0
    plan = wm.plan_raise(cc)
    assert plan.ceiling_tokens == 600_000
    assert plan.record is not None and plan.record["tokens"] == 580_800
    wm.install_watermark(cc, plan.record)
    second = wm.plan_raise(cc)
    # 580,800 -> min(600,000, 677,600) = 600,000: only 19,200 of headroom < 48,400 -> refused.
    assert second.record is None


def test_a_configured_threshold_tokens_cap_is_exceeded_session_wins():
    cc = make_compressor(1_000_000, threshold_tokens_cap=300_000)
    assert cc.threshold_tokens == 300_000
    cc.last_prompt_tokens = 0
    plan = wm.plan_raise(cc)
    assert plan.record is not None and plan.record["tokens"] == 300_000 + 96_800
    wm.install_watermark(cc, plan.record)
    assert cc.threshold_tokens == 396_800


def test_last_prompt_tokens_above_the_trigger_moves_the_basis():
    cc = make_compressor(1_000_000)
    cc.last_prompt_tokens = 700_000
    plan = wm.plan_raise(cc)
    assert plan.record is not None and plan.record["tokens"] == 796_800


def test_raise_only_a_low_record_never_lowers_the_trigger():
    cc = make_compressor(1_000_000)
    wm.install_watermark(cc, record_for(cc, 100_000))
    assert cc.threshold_tokens == 484_000


def test_hand_edited_record_above_the_ceiling_is_clamped():
    cc = make_compressor(1_000_000)
    wm.install_watermark(cc, record_for(cc, 5_000_000))
    assert cc.threshold_tokens == 871_200
