"""Per-session compaction watermark and the compaction-only cancel ("Keep full context").

Spec t_07c75c42 (operator-approved build t_5e9afac8). ``watermark`` owns the per-session record
``sessions.model_config["fork_compaction_watermark"]`` and the ``session-compaction-watermark`` anchor
target in ``ContextCompressor.threshold_tokens``; ``defer`` cancels an automatic compaction's commit fence
without any of ``hard_interrupt()``'s turn-wide fan-out and raises the watermark in the same step.
"""
