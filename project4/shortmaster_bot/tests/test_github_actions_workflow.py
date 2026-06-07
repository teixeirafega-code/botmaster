from __future__ import annotations

from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[3] / ".github" / "workflows" / "shortmaster-scheduled.yml"


def test_manual_workflow_defaults_to_paper_and_requires_explicit_real_upload() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "upload_mode:" in text
    assert "type: choice" in text
    assert "default: paper" in text
    assert "- real_upload" in text
    assert "inputs.upload_mode == 'real_upload'" in text
    assert "PAPER_MODE:" in text
    assert "Validate real upload secrets" in text
    assert "Require real upload for manual real_upload runs" in text
    assert "manual real_upload did not upload a video" in text


def test_workflow_summary_uses_heredoc_not_shell_backtick_command_substitution() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "python - <<'PY'" in text
    assert "```json" in text
    assert "python -c \"import json,pathlib,os" not in text
