"""Actions a rule can run: email, ntfy, generic JSON webhook."""
from __future__ import annotations

import asyncio
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

import httpx

from .config import Settings

NO_TEXT = "[No transcript. Listen to the recording.]"


class ActionError(Exception):
    def __init__(self, msg: str, retry: bool = True):
        super().__init__(msg)
        self.retry = retry


def render(template: str, ctx: dict) -> str:
    """Fill {name} placeholders. Unknown names are left alone; nothing is evaluated."""
    out = template
    for k, v in ctx.items():
        out = out.replace("{" + k + "}", str(v))
    return out


def context(text: str, rule_name: str, username: str, recorded_at: float | None) -> dict:
    t = time.localtime(recorded_at or time.time())
    words = text.split()
    first = " ".join(words[:8]) + ("…" if len(words) > 8 else "")
    return {
        "text": text or NO_TEXT,
        "first_words": first or "voice recording",
        "date": time.strftime("%Y-%m-%d", t),
        "time": time.strftime("%H:%M", t),
        "rule": rule_name,
        "user": username,
    }


def _one_line(s: str, limit: int = 150) -> str:
    return " ".join(s.split())[:limit]


# email -------------------------------------------------------------------
def _send_smtp(s: Settings, msg: EmailMessage):
    host, port, sec = s.get("smtp_host"), s.int("smtp_port"), s.get("smtp_security")
    try:
        if sec == "ssl":
            smtp = smtplib.SMTP_SSL(host, port, timeout=30, context=ssl.create_default_context())
        else:
            smtp = smtplib.SMTP(host, port, timeout=30)
        with smtp:
            if sec == "starttls":
                smtp.starttls(context=ssl.create_default_context())
            if s.get("smtp_user"):
                smtp.login(s.get("smtp_user"), s.get("smtp_password"))
            smtp.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        raise ActionError(f"SMTP login failed: {e.smtp_code}", False)
    except smtplib.SMTPRecipientsRefused as e:
        raise ActionError(f"recipient refused: {e}", False)
    except (smtplib.SMTPException, OSError) as e:
        raise ActionError(f"SMTP error: {type(e).__name__}: {e}")


async def send_email(s: Settings, cfg: dict, ctx: dict, user_email: str,
                     audio: Path | None, audio_mime: str | None, note: str) -> str:
    if not s.get("smtp_host"):
        raise ActionError("no SMTP server is set (Settings, Email)", False)
    to = (cfg.get("to") or s.get("note_to") or user_email or "").strip()
    if not to:
        raise ActionError("no address to send to (Settings, Email)", False)
    sender = s.get("mail_from") or s.get("smtp_user") or to
    msg = EmailMessage()
    msg["From"], msg["To"] = sender, to
    msg["Subject"] = _one_line(render(cfg.get("subject") or "{first_words}", ctx))
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1] if "@" in sender else None)
    body = ctx["text"]
    if note:
        body += f"\n\n--\n{note}"
    msg.set_content(body + "\n")
    attach = cfg.get("attach_audio") or ctx["text"] == NO_TEXT
    if attach and audio and audio.exists():
        main, _, sub = (audio_mime or "audio/mp4").partition("/")
        msg.add_attachment(audio.read_bytes(), maintype=main or "audio", subtype=sub or "mp4",
                           filename=f"recording-{ctx['date']}-{ctx['time'].replace(':', '')}{audio.suffix}")
    await asyncio.to_thread(_send_smtp, s, msg)
    return f"sent to {to}"


# ntfy ----------------------------------------------------------------------
async def send_ntfy(s: Settings, cfg: dict, ctx: dict, transport=None) -> str:
    topic = (cfg.get("topic") or s.get("ntfy_topic")).strip()
    if not topic:
        raise ActionError("no ntfy topic is set", False)
    payload = {"topic": topic, "message": ctx["text"],
               "title": _one_line(render(cfg.get("title") or "{rule}", ctx))}
    if str(cfg.get("priority") or "").isdigit():
        payload["priority"] = int(cfg["priority"])
    headers = {"Authorization": f"Bearer {s.get('ntfy_token')}"} if s.get("ntfy_token") else {}
    await _post(s.get("ntfy_url").rstrip("/") + "/", payload, headers, transport)
    return f"published to {topic}"


# webhook -------------------------------------------------------------------
async def send_webhook(cfg: dict, ctx: dict, extra: dict, transport=None) -> str:
    url = (cfg.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise ActionError("the webhook URL must start with http:// or https://", False)
    headers = {}
    for line in (cfg.get("headers") or "").splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip()] = v.strip()
    await _post(url, {**ctx, **extra}, headers, transport)
    return f"posted to {url.split('?')[0]}"


async def _post(url: str, payload: dict, headers: dict, transport=None):
    try:
        async with httpx.AsyncClient(timeout=30, transport=transport) as c:
            r = await c.post(url, json=payload, headers=headers)
    except httpx.HTTPError as e:
        raise ActionError(f"{type(e).__name__}: {e}")
    if r.status_code >= 300:
        retry = r.status_code >= 500 or r.status_code in (408, 429)
        raise ActionError(f"HTTP {r.status_code}: {r.text[:200]}", retry)
