# Auto-assembled sync_bot — loads body from .sync_bot_part_*.py
from pathlib import Path
_parts = sorted(Path(__file__).parent.glob(".sync_bot_part_*.py"))
if not _parts:
    raise SystemExit("Missing .sync_bot_part_*.py fragments — sync broken")
_code = "".join(p.read_text() for p in _parts)
exec(compile(_code, "sync_bot_assembled.py", "exec"), globals())
