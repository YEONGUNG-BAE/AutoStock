from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from bench_ollama_json import _smoke_messages


def test_smoke_messages_forbid_markdown_code_fences() -> None:
    messages = _smoke_messages()

    system_content = messages[0]["content"]
    user_content = messages[1]["content"]

    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert "Do not use code fences." in system_content
    assert "The first character of your response must be {" in system_content
    assert "without markdown or code fences" in user_content
    assert '"action": "HOLD"' in user_content
