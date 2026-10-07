"""Shared fixtures for the per-session compaction watermark tests (spec t_07c75c42 §6)."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from agent.context_compressor import ContextCompressor
from hermes_fork.compaction import watermark as wm


def make_compressor(window: int = 1_000_000, *, max_tokens: int | None = 32_000, threshold: float = 0.50,
                    **kw: Any) -> ContextCompressor:
    defaults: dict[str, Any] = dict(model="test/model", quiet_mode=True, threshold_percent=threshold,
                                    max_tokens=max_tokens)
    defaults.update(kw)
    with patch("agent.context_compressor.get_model_context_length", return_value=window):
        cc = ContextCompressor(**defaults)
        _ = cc.context_length  # resolve inside the patch
    return cc


def record_for(cc: ContextCompressor, tokens: int, *, session_id: str | None = None,
               context_length: int | None = None) -> dict:
    return {"v": 1, "tokens": tokens, "context_length": context_length or cc.context_length,
            "base_threshold_tokens": None, "ceiling_tokens": None, "source": "defer", "updated_at": 0.0,
            "session_id": cc._session_id if session_id is None else session_id}


@pytest.fixture(autouse=True)
def _drain_executor():
    yield
    ex = wm._executor
    if ex is not None:
        ex.submit(lambda: None).result(timeout=5)
