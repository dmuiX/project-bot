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
import re
import smtplib
import ssl
import sys
from typing import Any, Dict, List, Optional

from logging_config import setup_logging
from state_manager import ProjectStateManager

setup_logging()
logger = logging.getLogger(__name__)

DEFAULT_LEDGER = Path("data/notified_projects.json")


def read_score_from_file(filepath: str) -> Optional[int]:
    if not filepath:
        return None
    try:
        content = Path(filepath).read_text(encoding="utf-8")
    except OSError:
        return None
    llm_match = re.search(r"\*\*LLM Score:\*\*\s*(\d+)%", content)
    if llm_match and int(llm_match.group(1)) > 0:
        return int(llm_match.group(1))
    pre_match = re.search(r"\*\*Pre-Evaluation Score:\*\*\s*(-?\d+)%", content)
    if pre_match:
        return int(pre_match.group(1))
    return None


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
        meta = proj.get("metadata") or proj
        score = read_score_from_file(filepath)
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


def _render_card_html(item: Dict[str, Any]) -> str:
    score_val = item["score"]
    score_str = f"{score_val}%" if score_val is not None else "—"
    if score_val is not None and score_val >= 40:
        badge_bg, badge_fg = "#dcfce7", "#166534"
    elif score_val is not None and score_val >= 20:
        badge_bg, badge_fg = "#dbeafe", "#1e40af"
    else:
        badge_bg, badge_fg = "#f1f5f9", "#334155"

    title_html = html.escape(item["title"])
    company_html = html.escape(item["company"])
    provider_html = html.escape(item["provider"])
    snippet_html = (
        f"<p style='margin:12px 0 0;color:#475569;font-size:13px;line-height:1.5;'>"
        f"{html.escape(item['snippet'])}</p>"
        if item.get("snippet")
        else ""
    )
    cta_html = (
        f"<div style='margin-top:14px;'><a href='{html.escape(item['url'], quote=True)}' "
        "style='display:inline-block;padding:8px 14px;background:#2563eb;color:#ffffff;"
        "text-decoration:none;border-radius:7px;font-weight:600;font-size:13px;'>"
        "Ausschreibung öffnen →</a></div>"
        if item["url"]
        else ""
    )
    return (
        "<div style='background:#ffffff;border:1px solid #e2e8f0;border-radius:10px;"
        "padding:18px 20px;margin-bottom:14px;'>"
        "<table role='presentation' width='100%' cellspacing='0' cellpadding='0'><tr>"
        f"<td><h3 style='margin:0;font-size:16px;line-height:1.35;color:#0f172a;'>{title_html}</h3></td>"
        f"<td align='right' valign='top' style='padding-left:12px;white-space:nowrap;'>"
        f"<span style='display:inline-block;padding:3px 10px;border-radius:999px;"
        f"background:{badge_bg};color:{badge_fg};font-size:12px;font-weight:700;'>Match {html.escape(score_str)}</span></td>"
        "</tr></table>"
        f"<p style='margin:8px 0 0;font-size:12px;color:#64748b;'>"
        f"<strong style='color:#334155;'>Firma:</strong> {company_html} &nbsp;·&nbsp; "
        f"<strong style='color:#334155;'>Quelle:</strong> {provider_html}</p>"
        f"{snippet_html}"
        f"{cta_html}"
        "</div>"
    )


def build_digest_email(
    items: List[Dict[str, Any]],
    sender: str,
    recipients: List[str],
    dashboard_url: str = "",
) -> EmailMessage:
    count = len(items)
    if count == 1:
        item = items[0]
        score_str = f"{item['score']}%" if item["score"] is not None else "—"
        subject = f"[project-bot] {score_str} · {item['title']}"
        header_sub = "1 neues passendes Projekt"
    else:
        subject = f"[project-bot] {count} neue passende Projekte"
        header_sub = f"{count} neue passende Projekte"

    plain_lines = [f"{header_sub}:\n"]
    for item in items:
        score_str = f"{item['score']}%" if item["score"] is not None else "—"
        plain_lines.append(
            f"- [{score_str}] {item['title']}\n"
            f"  Firma: {item['company']} | Quelle: {item['provider']}"
        )
        if item.get("snippet"):
            plain_lines.append(f"  {item['snippet']}")
        if item["url"]:
            plain_lines.append(f"  Link: {item['url']}")
        plain_lines.append("")
    if dashboard_url:
        plain_lines.append(f"Dashboard: {dashboard_url}")

    cards_html = "".join(_render_card_html(item) for item in items)
    dash_footer = (
        f"<div style='text-align:center;margin-top:8px;'>"
        f"<a href='{html.escape(dashboard_url, quote=True)}' "
        "style='display:inline-block;padding:10px 18px;background:#0f172a;color:#f8fafc;"
        "text-decoration:none;border-radius:8px;font-weight:600;font-size:13px;'>"
        "Alle Projekte im Dashboard öffnen →</a></div>"
        if dashboard_url
        else ""
    )

    html_body = (
        "<!DOCTYPE html><html><body style='margin:0;padding:24px;background:#f1f5f9;"
        "font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#0f172a;'>"
        "<table role='presentation' width='100%' cellspacing='0' cellpadding='0' style='max-width:640px;margin:0 auto;'>"
        "<tr><td style='background:#0f172a;color:#f8fafc;padding:16px 22px;border-radius:12px;margin-bottom:16px;'>"
        "<table role='presentation' width='100%' cellspacing='0' cellpadding='0'><tr>"
        "<td style='font-size:13px;font-weight:700;letter-spacing:0.05em;text-transform:uppercase;color:#94a3b8;'>project-bot</td>"
        f"<td align='right' style='font-size:13px;font-weight:600;color:#e2e8f0;'>{html.escape(header_sub)}</td>"
        "</tr></table>"
        "</td></tr>"
        f"<tr><td style='padding-top:16px;'>{cards_html}{dash_footer}</td></tr>"
        "</table></body></html>"
    )

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content("\n".join(plain_lines))
    msg.add_alternative(html_body, subtype="html")
    return msg


def send_digest_email(
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
) -> EmailMessage:
    msg = build_digest_email(items, sender=sender, recipients=recipients, dashboard_url=dashboard_url)
    context = ssl.create_default_context()
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
        server.send_message(msg)

    notified_ids.update(item["id"] for item in items)
    save_notified_ids(ledger_path, notified_ids)
    return msg


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

    msg = send_digest_email(
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
    summary = (
        f"📧 E-Mail-Benachrichtigung versendet ({len(items)} Projekt(e)) "
        f"an {', '.join(recipients)} — Betreff: {msg['Subject']}"
    )
    logger.info(summary)
    print(f"✅ {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
