"""Unit tests for notify_accepted.py (SMTP individual email notifications)."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import notify_accepted


def _write_project(directory: Path, name: str, state: str = "accepted", pre_score: int = 41) -> Path:
    path = directory / f"{name}.md"
    path.write_text(
        f"---\n"
        f"title: Cloud DevSecOps Engineer\n"
        f"company: Acme GmbH\n"
        f"provider_name: Freelancermap\n"
        f"source_url: https://example.com/project/{name}\n"
        f"state: {state}\n"
        f"---\n\n"
        f"# Cloud DevSecOps Engineer\n\n"
        f"## Beschreibung\n"
        f"Kubernetes, ArgoCD and Terraform platform engineering role.\n\n"
        f"## Evaluation\n"
        f"**Pre-Evaluation Score:** {pre_score}%\n"
        f"**LLM Score:** 0%\n",
        encoding="utf-8",
    )
    return path


def test_read_score_prefers_positive_llm_otherwise_pre_eval(tmp_path):
    p1 = _write_project(tmp_path, "proj1", pre_score=43)
    assert notify_accepted.read_score_from_file(str(p1)) == 43

    p2 = tmp_path / "proj2.md"
    p2.write_text("**Pre-Evaluation Score:** 40%\n**LLM Score:** 88%\n", encoding="utf-8")
    assert notify_accepted.read_score_from_file(str(p2)) == 88


def test_build_project_email_subject_and_html():
    item = {
        "id": "p1",
        "title": "Kubernetes Platform Engineer",
        "company": "Acme GmbH",
        "provider": "Freelancermap",
        "url": "https://example.com/p1",
        "score": 41,
        "snippet": "Remote K8s & Terraform project.",
    }
    msg = notify_accepted.build_project_email(
        item,
        sender="me@example.com",
        recipients=["me@example.com"],
        dashboard_url="https://bot.example.local",
    )
    assert msg["Subject"] == "[project-bot] 41% · Kubernetes Platform Engineer"
    assert msg["From"] == "me@example.com"
    assert msg["To"] == "me@example.com"
    html_part = msg.get_body(preferencelist=("html",)).get_content()
    assert "Match 41%" in html_part
    assert "Acme GmbH" in html_part
    assert "https://example.com/p1" in html_part
    assert "https://bot.example.local" in html_part


def test_main_sends_once_using_email_server_fallback_and_deduplicates(tmp_path, monkeypatch):
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    _write_project(projects_dir, "accepted_1", state="accepted", pre_score=41)
    _write_project(projects_dir, "rejected_1", state="rejected", pre_score=0)
    ledger = tmp_path / "notified.json"

    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SMTP_USERNAME", raising=False)
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    monkeypatch.delenv("NOTIFY_EMAIL_TO", raising=False)
    monkeypatch.setenv("EMAIL_SERVER", "mail.example.com")
    monkeypatch.setenv("EMAIL_USERNAME", "me@example.com")
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")

    mock_smtp_instance = MagicMock()
    mock_smtp_cm = MagicMock()
    mock_smtp_cm.__enter__.return_value = mock_smtp_instance

    with patch("notify_accepted.smtplib.SMTP", return_value=mock_smtp_cm) as mock_smtp:
        monkeypatch.setattr(
            "sys.argv",
            ["notify_accepted.py", "--projects-dir", str(projects_dir), "--ledger", str(ledger)],
        )
        rc1 = notify_accepted.main()
        rc2 = notify_accepted.main()

    assert rc1 == 0
    assert rc2 == 0
    mock_smtp.assert_called_once_with("mail.example.com", 587, timeout=30)
    mock_smtp_instance.starttls.assert_called_once()
    mock_smtp_instance.login.assert_called_once_with("me@example.com", "secret")
    assert mock_smtp_instance.send_message.call_count == 1
    assert "accepted_1" in notify_accepted.load_notified_ids(ledger)
