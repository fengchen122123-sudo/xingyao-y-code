import json

from core.share import export_session_html, export_session_json, export_session_markdown


class FakeMemory:
    def get_history(self, _session):
        return [
            {"role": "system", "content": "hidden"},
            {
                "role": "user",
                "content": "Bearer abc.def.ghi sk-live-1234567890",
                "reasoning_content": "private reasoning",
            },
        ]


def test_json_share_export_removes_reasoning_and_common_secrets(tmp_path, monkeypatch):
    monkeypatch.setattr("core.model_manager.load_config", lambda: {"providers": []})
    output = tmp_path / "share.json"

    export_session_json("session", manager=FakeMemory(), output_path=output)

    raw = output.read_text(encoding="utf-8")
    data = json.loads(raw)
    assert "reasoning_content" not in data["messages"][0]
    for secret in ("abc.def.ghi", "sk-live-1234567890", "private reasoning"):
        assert secret not in raw


def test_markdown_and_html_share_exports_redact_common_secrets(tmp_path, monkeypatch):
    monkeypatch.setattr("core.model_manager.load_config", lambda: {"providers": []})
    markdown = tmp_path / "share.md"
    html = tmp_path / "share.html"

    export_session_markdown("session", manager=FakeMemory(), output_path=markdown)
    export_session_html("session", manager=FakeMemory(), output_path=html)

    for output in (markdown, html):
        raw = output.read_text(encoding="utf-8")
        assert "abc.def.ghi" not in raw
        assert "sk-live-1234567890" not in raw
