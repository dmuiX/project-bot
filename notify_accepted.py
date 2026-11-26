#!/usr/bin/env python3
"""Send an SMTP email digest for newly accepted projects.

Reads projects in state "accepted" from the projects directory, filters out
already-notified project IDs stored in ``data/notified_projects.json``, and
sends a single summary email (plain text + HTML) via SMTP.

Environment variables:
  SMTP_HOST         SMTP server hostname (required to send; skips cleanly if unset)
  SMTP_PORT         SMTP port (default: 587; uses implicit SSL on 465, STARTTLS otherwise)
  SMTP_USERNAME     SMTP login username (falls back to EMAIL_USERNAME)
  SMTP_PASSWORD     SMTP login password (falls back to EMAIL_PASSWORD)
  NOTIFY_EMAIL_TO   Recipient address(es), comma-separated (falls back to SMTP_USERNAME)
  NOTIFY_EMAIL_FROM Sender address (falls back to SMTP_USERNAME)
  DASHBOARD_URL     Optional link to the Project Bot web dashboard
"""

from __future__ import annotations

import argparse
from datetime import datetime
from email.message import EmailMessage
import html
import json
import logging
import os
from pathlib import Path
import smtplib
import ssl
import sys
from typing import Any, Dict, List

from logging_config import setup_logging
from scores import read_pre_eval_score_from_file
from state_manager import ProjectStateManager

setup_logging()
logger = logging.getLogger(__name__)

DEFAULT_LEDGER = Path("data/notified_projects.json")


def load_notified_ids(ledger_path: Path) -> set[str]:
    if not ledger_path.exists():
        return set()
    try:
        data = json.loads(ledger_path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("notified_ids"), list):
            return set(str(i) for i in data["notified_ids"])
    except Exception as exc:
        logger.warning("Could not read %s: %s", ledger_path, exc)
    return set()


def save_notified_ids(ledger_path: Path, notified_ids: set[str]) -> None:
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "notified_ids": sorted(notified_ids),
        "updated_at": datetime.now().isoformat(),
    }
    ledger_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def collect_new_accepted(projects_dir: str, notified_ids: set[str]) -> List[Dict[str, Any]]:
    manager = ProjectStateManager(projects_dir)
    accepted = manager.get_projects_by_state("accepted")
    items: List[Dict[str, Any]] = []
    for proj in accepted:
        filepath = proj.get("path", "")
        project_id = Path(filepath).stem if filepath else proj.get("id", "")
        if not project_id or project_id in notified_ids:
            continue
        meta = proj.get("metadata") or {}
        score = read_pre_eval_score_from_file(filepath) if filepath else None
        company = meta.get("company")
        if company in (None, "", "None"):
            company = "—"
        items.append({
            "id": project_id,
            "title": meta.get("title") or project_id,
            "company": str(company),
            "provider": meta.get("provider_name") or meta.get("provider_id") or "—",
            "url": meta.get("source_url") or "",
            "score": score,
        })
    items.sort(key=lambda x: (x["score"] is None, -(x["score"] or 0), x["title"]))
    return items


def build_email(
    items: List[Dict[str, Any]],
    sender: str,
    recipients: List[str],
    dashboard_url: str = "",
) -> EmailMessage:
    count = len(items)
    subject = f"[Project Bot] {count} {'neues passendes Projekt' if count == 1 else 'neue passende Projekte'}"

    plain_lines = [f"{count} neue passende Projekte gefunden:\n"]
    html_rows = []
    for item in items:
        score_str = f"{item['score']}%" if item["score"] is not None else "—"
        plain_lines.append(
            f"- [{score_str}] {item['title']}\n"
            f"  Firma: {item['company']} | Quelle: {item['provider']}\n"
            f"  Link: {item['url']}\n"
        )
        title_html = html.escape(item["title"])
        if item["url"]:
            title_cell = f'<a href="{html.escape(item["url"], quote=True)}">{title_html}</a>'
        else:
            title_cell = title_html
        html_rows.append(
            f"<tr>"
            f"<td style='padding:6px 10px;font-weight:bold;white-space:nowrap;'>{html.escape(score_str)}</td>"
            f"<td style='padding:6px 10px;'>{title_cell}</td>"
            f"<td style='padding:6px 10px;'>{html.escape(item['company'])}</td>"
            f"<td style='padding:6px 10px;'>{html.escape(item['provider'])}</td>"
            f"</tr>"
        )

    if dashboard_url:
        plain_lines.append(f"\nDashboard: {dashboard_url}")
        footer_html = f'<p style="margin-top:16px;"><a href="{html.escape(dashboard_url, quote=True)}">Im Dashboard öffnen →</a></p>'
    else:
        footer_html = ""

    html_body = (
        "<html><body style='font-family:sans-serif;font-size:14px;color:#222;'>"
        f"<h3>{html.escape(subject)}</h3>"
        "<table border='1' cellspacing='0' cellpadding='0' style='border-collapse:collapse;border-color:#ddd;'>"
        "<thead><tr style='background:#f4f4f4;'>"
        "<th style='padding:6px 10px;text-align:left;'>Score</th>"
        "<th style='padding:6px 10px;text-align:left;'>Projekt</th>"
        "<th style='padding:6px 10px;text-align:left;'>Firma</th>"
        "<th style='padding:6px 10px;text-align:left;'>Quelle</th>"
        "</tr></thead>"
        f"<tbody>{''.join(html_rows)}</tbody>"
        "</table>"
        f"{footer_html}"
        "</body></html>"
    )

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content("\n".join(plain_lines))
    msg.add_alternative(html_body, subtype="html")
    return msg


def send_smtp(msg: EmailMessage, host: str, port: int, username: str, password: str) -> None:
    context = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30, context=context) as server:
            if username and password:
                server.login(username, password)
            server.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            if username and password:
                server.login(username, password)
            server.send_message(msg)


def main() -> int:
    parser = argparse.ArgumentParser(description="Email digest for newly accepted Project Bot items")
    parser.add_argument("--projects-dir", default="projects", help="Directory containing project markdown files")
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER), help="Path to notified-projects ledger JSON")
    parser.add_argument("--dry-run", action="store_true", help="Print notification summary without sending email")
    args = parser.parse_args()

    ledger_path = Path(args.ledger)
    notified_ids = load_notified_ids(ledger_path)
    items = collect_new_accepted(args.projects_dir, notified_ids)

    if not items:
        print("ℹ️  No new accepted projects to notify.")
        return 0

    host = os.environ.get("SMTP_HOST", "").strip()
    raw_port = os.environ.get("SMTP_PORT", "587").strip()
    port = int(raw_port) if raw_port.isdigit() else 587
    username = (os.environ.get("SMTP_USERNAME") or os.environ.get("EMAIL_USERNAME") or "").strip()
    password = (os.environ.get("SMTP_PASSWORD") or os.environ.get("EMAIL_PASSWORD") or "").strip()
    recipients_raw = (os.environ.get("NOTIFY_EMAIL_TO") or username).strip()
    sender = (os.environ.get("NOTIFY_EMAIL_FROM") or username).strip()
    dashboard_url = os.environ.get("DASHBOARD_URL", "").strip()

    recipients = [r.strip() for r in recipients_raw.split(",") if r.strip()]

    if args.dry_run:
        print(f"🧪 DRY RUN: Would email {len(items)} accepted projects to {recipients or ['<unset>']}:")
        for item in items:
            print(f"  - [{item['score']}%] {item['title']} ({item['company']})")
        return 0

    if not host or not sender or not recipients:
        print("⏭️  Skipping email notification (SMTP_HOST / NOTIFY_EMAIL_TO not configured).")
        return 0

    msg = build_email(items, sender=sender, recipients=recipients, dashboard_url=dashboard_url)
    send_smtp(msg, host=host, port=port, username=username, password=password)

    notified_ids.update(item["id"] for item in items)
    save_notified_ids(ledger_path, notified_ids)
    print(f"✅ Sent email notification for {len(items)} accepted project(s) to {', '.join(recipients)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
