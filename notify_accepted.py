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


def _extract_snippet(filepath: str, max_len: int = 320) -> str:
    if not filepath:
        return ""
    try:
        text = Path(filepath).read_text(encoding="utf-8")
    except Exception:
        return ""
    marker = "## Beschreibung"
    idx = text.find(marker)
    body = text[idx + len(marker):] if idx != -1 else text
    # Stop before evaluation appendix if present
    for stop in ("\n## ", "\n---"):
        s_idx = body.find(stop)
        if s_idx != -1:
            body = body[:s_idx]
    cleaned = " ".join(body.split())
    if len(cleaned) > max_len:
        return cleaned[: max_len - 1].rstrip() + "…"
    return cleaned


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
            "snippet": _extract_snippet(filepath),
        })
    items.sort(key=lambda x: (x["score"] is None, -(x["score"] or 0), x["title"]))
    return items


def build_project_email(
    item: Dict[str, Any],
    sender: str,
    recipients: List[str],
    dashboard_url: str = "",
) -> EmailMessage:
    score_val = item["score"]
    score_str = f"{score_val}%" if score_val is not None else "—"
    subject = f"[project-bot] {score_str} · {item['title']}"

    plain_lines = [
        f"Projekt: {item['title']}",
        f"Score:   {score_str}",
        f"Firma:   {item['company']}",
        f"Quelle:  {item['provider']}",
    ]
    if item.get("snippet"):
        plain_lines.append(f"\n{item['snippet']}")
    if item["url"]:
        plain_lines.append(f"\nAusschreibung: {item['url']}")
    if dashboard_url:
        plain_lines.append(f"Dashboard:     {dashboard_url}")

    title_html = html.escape(item["title"])
    company_html = html.escape(item["company"])
    provider_html = html.escape(item["provider"])
    snippet_html = (
        f"<p style='margin:16px 0 0;color:#475569;font-size:14px;line-height:1.55;'>"
        f"{html.escape(item['snippet'])}</p>"
        if item.get("snippet")
        else ""
    )

    if score_val is not None and score_val >= 40:
        badge_bg, badge_fg = "#dcfce7", "#166534"
    elif score_val is not None and score_val >= 20:
        badge_bg, badge_fg = "#dbeafe", "#1e40af"
    else:
        badge_bg, badge_fg = "#f1f5f9", "#334155"

    cta_buttons = []
    if item["url"]:
        cta_buttons.append(
            f"<a href='{html.escape(item['url'], quote=True)}' "
            "style='display:inline-block;padding:10px 18px;background:#2563eb;color:#ffffff;"
            "text-decoration:none;border-radius:8px;font-weight:600;font-size:14px;margin-right:10px;'>"
            "Ausschreibung öffnen →</a>"
        )
    if dashboard_url:
        cta_buttons.append(
            f"<a href='{html.escape(dashboard_url, quote=True)}' "
            "style='display:inline-block;padding:10px 16px;background:#f1f5f9;color:#0f172a;"
            "text-decoration:none;border-radius:8px;font-weight:600;font-size:14px;'>"
            "Dashboard</a>"
        )
    cta_block = f"<div style='margin-top:22px;'>{''.join(cta_buttons)}</div>" if cta_buttons else ""

    html_body = (
        "<!DOCTYPE html><html><body style='margin:0;padding:24px;background:#f8fafc;"
        "font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#0f172a;'>"
        "<table role='presentation' width='100%' cellspacing='0' cellpadding='0' style='max-width:620px;margin:0 auto;'>"
        "<tr><td style='background:#0f172a;color:#f8fafc;padding:14px 22px;border-radius:12px 12px 0 0;'>"
        "<table role='presentation' width='100%' cellspacing='0' cellpadding='0'><tr>"
        "<td style='font-size:13px;font-weight:700;letter-spacing:0.04em;text-transform:uppercase;color:#94a3b8;'>project-bot</td>"
        f"<td align='right'><span style='display:inline-block;padding:4px 10px;border-radius:999px;"
        f"background:{badge_bg};color:{badge_fg};font-size:12px;font-weight:700;'>Match {html.escape(score_str)}</span></td>"
        "</tr></table>"
        "</td></tr>"
        "<tr><td style='background:#ffffff;padding:24px 22px;border:1px solid #e2e8f0;border-top:none;border-radius:0 0 12px 12px;'>"
        f"<h2 style='margin:0 0 14px;font-size:19px;line-height:1.35;color:#0f172a;'>{title_html}</h2>"
        "<table role='presentation' cellspacing='0' cellpadding='0' style='width:100%;background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;'>"
        "<tr>"
        f"<td style='padding:10px 14px;font-size:13px;color:#475569;'><strong style='color:#0f172a;'>Firma:</strong> {company_html}</td>"
        f"<td style='padding:10px 14px;font-size:13px;color:#475569;'><strong style='color:#0f172a;'>Quelle:</strong> {provider_html}</td>"
        "</tr></table>"
        f"{snippet_html}"
        f"{cta_block}"
        "</td></tr>"
        "</table></body></html>"
    )

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content("\n".join(plain_lines))
    msg.add_alternative(html_body, subtype="html")
    return msg


def send_individual_emails(
    items: List[Dict[str, Any]],
    host: str,
    port: int,
    username: str,
    password: str,
    sender: str,
    recipients: List[str],
    dashboard_url: str,
    notified_ids: set[str],
    ledger_path: Path,
) -> int:
    context = ssl.create_default_context()
    sent = 0
    if port == 465:
        conn_cm = smtplib.SMTP_SSL(host, port, timeout=30, context=context)
    else:
        conn_cm = smtplib.SMTP(host, port, timeout=30)

    with conn_cm as server:
        if port != 465:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
        if username and password:
            server.login(username, password)
        for item in items:
            msg = build_project_email(item, sender=sender, recipients=recipients, dashboard_url=dashboard_url)
            server.send_message(msg)
            notified_ids.add(item["id"])
            save_notified_ids(ledger_path, notified_ids)
            sent += 1
    return sent


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

    host = (os.environ.get("SMTP_HOST") or os.environ.get("EMAIL_SERVER") or "").strip()
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

    sent = send_individual_emails(
        items=items,
        host=host,
        port=port,
        username=username,
        password=password,
        sender=sender,
        recipients=recipients,
        dashboard_url=dashboard_url,
        notified_ids=notified_ids,
        ledger_path=ledger_path,
    )
    print(f"✅ Sent {sent} individual email notification(s) to {', '.join(recipients)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
