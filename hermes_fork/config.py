"""Fork-owned opt-in settings; never migrate upstream config versions."""
from pathlib import Path
import math


def human_input_bridge(config, *, home):
    raw = config.get("human_input_bridge") or {}
    if raw.get("enabled") is not True:
        return None
    recipient = dict(raw.get("telegram") or {})
    recipient.setdefault("thread_id", None)
    for key in ("user_id", "chat_id", "bot_id"):
        if type(recipient.get(key)) is not int:
            raise ValueError(f"human_input_bridge.telegram.{key} must be an integer")
    if recipient["thread_id"] is not None and type(recipient["thread_id"]) is not int:
        raise ValueError("human_input_bridge.telegram.thread_id must be an integer or null")
    delay = raw.get("delay_seconds", 120)
    if type(delay) not in (int, float) or not math.isfinite(delay) or delay < 0:
        raise ValueError("human_input_bridge.delay_seconds must be finite and nonnegative")
    home = Path(home).resolve()
    path = Path(raw.get("mailbox_path") or home / "human-input-bridge" / "mailbox.sqlite3")
    if not path.is_absolute():
        path = home / path
    path = path.resolve()
    if not path.is_relative_to(home):
        raise ValueError("human_input_bridge.mailbox_path must stay inside the owning profile")
    return {**raw, "telegram": recipient, "delay_seconds": delay, "mailbox_path": path}
