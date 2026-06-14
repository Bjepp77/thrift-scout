from __future__ import annotations

import logging
import smtplib
import time
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from thrift_scout.config import Config

log = logging.getLogger(__name__)
_jinja = Environment(
    loader=FileSystemLoader(str(Path(__file__).parent / "templates")),
    autoescape=True,
)

_WRAP = (
    '<html><body style="font-family:-apple-system,BlinkMacSystemFont,'
    "'Segoe UI',sans-serif;background:#E8E0D0;padding:40px;\">"
    '<div style="max-width:600px;margin:0 auto;background:#FFF;'
    'border-radius:8px;padding:32px;{extra_style}">{body}</div></body></html>'
)


def render_report(matches: dict[str, list[dict]],
                   active_bids: list[dict] | None = None) -> str:
    return _jinja.get_template("report.html.j2").render(
        matches_by_brand=matches,
        active_bids=active_bids or [],
        generated_at=datetime.now().strftime("%B %d, %Y at %I:%M %p"),
        total_items=sum(len(v) for v in matches.values()),
    )


def render_empty_report() -> str:
    d = datetime.now().strftime("%B %d, %Y")
    return _WRAP.format(
        extra_style="text-align:center;",
        body=(
            '<h2 style="color:#7A8C6E;margin:0 0 12px;">Thrift Scout</h2>'
            '<p style="color:#8C8C7A;font-size:15px;margin:0;">'
            "Nothing new today. All quiet on the thrift front.</p>"
            f'<p style="color:#B8926A;font-size:12px;margin-top:16px;">'
            f"Daily heartbeat &mdash; {d}</p>"
        ),
    )


def render_error_report(errors: list[str]) -> str:
    li = "".join(f'<li style="color:#C0392B;margin:4px 0;">{e}</li>' for e in errors)
    ts = datetime.now().strftime("%B %d, %Y at %I:%M %p")
    return _WRAP.format(
        extra_style="",
        body=(
            '<h2 style="color:#C0392B;margin:0 0 12px;">Thrift Scout &mdash; Errors</h2>'
            f"<ul>{li}</ul>"
            f'<p style="color:#8C8C7A;font-size:12px;margin-top:16px;">{ts}</p>'
        ),
    )


def send_email(subject: str, html: str, config: Config, recipient: str) -> bool:
    if not all((config.email_sender, config.email_password, recipient)):
        log.warning("Missing email config — skipping.")
        return False
    msg = MIMEText(html, "html")
    msg["Subject"], msg["From"], msg["To"] = subject, config.email_sender, recipient
    for attempt in range(3):
        try:
            with smtplib.SMTP(config.smtp_host, config.smtp_port) as s:
                s.starttls()
                s.login(config.email_sender, config.email_password)
                s.send_message(msg)
            return True
        except Exception as e:
            if attempt == 2:
                log.error("Email failed after 3 attempts: %s", e)
                return False
            log.warning("Email attempt %d failed: %s — retrying", attempt + 1, e)
            time.sleep(2 ** attempt)
    return False
