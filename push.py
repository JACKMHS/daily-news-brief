"""
push.py - Multi-channel push delivery.

Channels
--------
  serverchan  Server酱 (FTQQ) - WeChat push via personal SCT key
  email       SMTP email (universal, easiest for non-tech users)
  wechat_oa   WeChat Official Account template messages (best China UX)
  wecom       WeCom (enterprise WeChat) webhook

Dispatcher
----------
  push(title, body, method, target)   - route to the right channel
  push_all_subscribers(...)           - called from main.py daily job
"""

from __future__ import annotations

import logging
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

import requests

import config

logger = logging.getLogger(__name__)

# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _post_form(url: str, data: dict, *, label: str) -> bool:
    delay = config.RETRY_BACKOFF
    for attempt in range(1, config.RETRY_ATTEMPTS + 1):
        try:
            resp = requests.post(url, data=data, timeout=config.REQUEST_TIMEOUT,
                                 headers={"User-Agent": "DailyBrief/1.0"})
            resp.raise_for_status()
            body = resp.json() if resp.content else {}
            # Server酱 v3 success: {"errno":0,...} or HTTP 200 with no errno key.
            # Treat any HTTP 200 response as success — if raise_for_status()
            # didn't throw, the message was accepted by the API.
            errno = body.get("errno")
            if errno is None or errno == 0:
                pushid = body.get("data", {}).get("pushid", "")
                logger.info("%s queued (attempt %d, pushid=%s).",
                            label, attempt, pushid)
                return True
            logger.error("%s API error: errno=%s errmsg=%s",
                         label, errno, body.get("errmsg"))
            return False
        except requests.RequestException as exc:
            logger.warning("%s attempt %d/%d: %s", label, attempt, config.RETRY_ATTEMPTS, exc)
            if attempt < config.RETRY_ATTEMPTS:
                time.sleep(delay); delay *= config.RETRY_BACKOFF
    logger.error("%s failed after %d attempts.", label, config.RETRY_ATTEMPTS)
    return False


def _post_json(url: str, payload: dict, *, label: str) -> bool:
    delay = config.RETRY_BACKOFF
    for attempt in range(1, config.RETRY_ATTEMPTS + 1):
        try:
            resp = requests.post(url, json=payload, timeout=config.REQUEST_TIMEOUT,
                                 headers={"Content-Type": "application/json; charset=utf-8",
                                          "User-Agent": "DailyBrief/1.0"})
            resp.raise_for_status()
            body = resp.json() if resp.content else {}
            errcode = body.get("errcode", body.get("errno", 0))
            if errcode == 0:
                logger.info("%s push succeeded (attempt %d).", label, attempt)
                return True
            logger.error("%s error: errcode=%s msg=%s", label, errcode, body.get("errmsg"))
            return False
        except requests.RequestException as exc:
            logger.warning("%s attempt %d/%d: %s", label, attempt, config.RETRY_ATTEMPTS, exc)
            if attempt < config.RETRY_ATTEMPTS:
                time.sleep(delay); delay *= config.RETRY_BACKOFF
    logger.error("%s failed after %d attempts.", label, config.RETRY_ATTEMPTS)
    return False


# ── Server酱 ──────────────────────────────────────────────────────────────────

_SC_URL = "https://sctapi.ftqq.com/{key}.send"

def push_serverchan(title: str, content: str, key: Optional[str] = None) -> bool:
    """Send via Server酱 SCT v3 API (form POST)."""
    send_key = key or config.SERVERCHAN_KEY
    if not send_key:
        logger.error("Server酱 key not configured.")
        return False
    first_line = next((l.strip() for l in content.splitlines() if l.strip()), "")
    data = {
        "title": title[:32],
        "desp":  content,
        "short": first_line[:64],
    }
    logger.info("Pushing via Server酱 (key=%.8s...) title=%r", send_key, title[:32])
    return _post_form(_SC_URL.format(key=send_key), data, label="Server酱")


# ── Email (SMTP) ──────────────────────────────────────────────────────────────

def _brief_to_html(title: str, body: str) -> str:
    """Wrap plain-text brief in a clean HTML email."""
    paragraphs = ""
    for line in body.splitlines():
        line = line.strip()
        if not line:
            paragraphs += "<br>"
        elif line.startswith("【") or line.startswith("="):
            paragraphs += f'<h2 style="color:#6C5CE7;border-bottom:2px solid #6C5CE7;padding-bottom:6px">{line}</h2>'
        elif line[:2].rstrip(". ").isdigit():
            paragraphs += f'<h3 style="margin-top:20px;color:#2d3436">{line}</h3>'
        elif line.startswith("Why it matters:"):
            paragraphs += f'<p style="background:#f0eeff;border-left:3px solid #6C5CE7;padding:8px 12px;border-radius:4px">{line}</p>'
        elif line.startswith("🔗"):
            href = line.replace("🔗 ", "")
            paragraphs += f'<p><a href="{href}" style="color:#6C5CE7">Read full article →</a></p>'
        else:
            paragraphs += f'<p style="color:#555;line-height:1.7">{line}</p>'

    return f"""
    <!DOCTYPE html><html><body style="font-family:-apple-system,sans-serif;max-width:620px;
    margin:0 auto;padding:24px;background:#fff">
    <div style="background:linear-gradient(135deg,#6C5CE7,#a29bfe);padding:28px 24px;
         border-radius:12px 12px 0 0;text-align:center">
      <h1 style="color:#fff;margin:0;font-size:22px">{title}</h1>
    </div>
    <div style="background:#fafafa;padding:28px 24px;border-radius:0 0 12px 12px;
         border:1px solid #e0e0e0;border-top:none">
      {paragraphs}
    </div>
    <p style="text-align:center;font-size:12px;color:#aaa;margin-top:18px">
      Daily Brief &nbsp;|&nbsp; You're receiving this because you subscribed.
    </p>
    </body></html>
    """


def push_email(title: str, content: str, to_address: str) -> bool:
    """
    Send the brief as a nicely formatted HTML email via SMTP.

    Reads EMAIL_HOST / EMAIL_PORT / EMAIL_USER / EMAIL_PASS from config.
    Uses TLS (STARTTLS on port 587) by default.
    """
    if not config.EMAIL_USER or not config.EMAIL_PASS:
        logger.error("Email credentials not configured (EMAIL_USER / EMAIL_PASS).")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"{config.EMAIL_SUBJECT} - {title}"
    msg["From"]    = config.EMAIL_FROM or config.EMAIL_USER
    msg["To"]      = to_address

    msg.attach(MIMEText(content, "plain", "utf-8"))
    msg.attach(MIMEText(_brief_to_html(title, content), "html", "utf-8"))

    delay = config.RETRY_BACKOFF
    for attempt in range(1, config.RETRY_ATTEMPTS + 1):
        try:
            with smtplib.SMTP(config.EMAIL_HOST, config.EMAIL_PORT,
                               timeout=config.REQUEST_TIMEOUT) as smtp:
                smtp.ehlo()
                smtp.starttls()
                smtp.login(config.EMAIL_USER, config.EMAIL_PASS)
                smtp.sendmail(msg["From"], [to_address], msg.as_string())
            logger.info("Email sent to %s (attempt %d).", to_address, attempt)
            return True
        except smtplib.SMTPAuthenticationError:
            logger.error("SMTP auth failed — check EMAIL_USER / EMAIL_PASS.")
            return False   # no point retrying auth errors
        except (smtplib.SMTPException, OSError) as exc:
            logger.warning("Email attempt %d/%d to %s: %s",
                           attempt, config.RETRY_ATTEMPTS, to_address, exc)
            if attempt < config.RETRY_ATTEMPTS:
                time.sleep(delay); delay *= config.RETRY_BACKOFF

    logger.error("Email to %s failed after %d attempts.", to_address, config.RETRY_ATTEMPTS)
    return False


# ── WeChat Official Account (公众号) ──────────────────────────────────────────

_WX_TOKEN_URL = "https://api.weixin.qq.com/cgi-bin/token"
_WX_SEND_URL  = "https://api.weixin.qq.com/cgi-bin/message/template/send"

# Module-level token cache
_wx_access_token: str = ""
_wx_token_expiry: float = 0.0


def _get_wx_access_token() -> str:
    """Fetch (or return cached) WeChat access token. Expires every 2 hours."""
    global _wx_access_token, _wx_token_expiry
    if time.time() < _wx_token_expiry - 60:
        return _wx_access_token

    try:
        resp = requests.get(_WX_TOKEN_URL, params={
            "grant_type": "client_credential",
            "appid":  config.WECHAT_APPID,
            "secret": config.WECHAT_APPSECRET,
        }, timeout=config.REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if "access_token" not in data:
            logger.error("WeChat token error: %s", data)
            return ""
        _wx_access_token = data["access_token"]
        _wx_token_expiry = time.time() + data.get("expires_in", 7200)
        logger.info("WeChat access token refreshed (expires in %ds).",
                    data.get("expires_in", 7200))
        return _wx_access_token
    except requests.RequestException as exc:
        logger.error("Failed to fetch WeChat access token: %s", exc)
        return ""


def push_wechat_oa(title: str, content: str, openid: str) -> bool:
    """
    Send a template message to a WeChat follower via Official Account API.

    Requires in .env:
        WECHAT_APPID, WECHAT_APPSECRET, WECHAT_TEMPLATE_ID

    The template must have at least two fields defined in WeChat MP backend:
        {{heading.DATA}}  - the brief title
        {{body.DATA}}     - the summary text (truncated to 200 chars for card)

    Users subscribe by scanning the OA QR code — no SCT key needed.
    """
    if not config.WECHAT_APPID or not config.WECHAT_APPSECRET:
        logger.error("WeChat OA credentials not configured.")
        return False

    token = _get_wx_access_token()
    if not token:
        return False

    # Truncate body for the card preview
    preview = content[:200].replace("\n", " ") + ("…" if len(content) > 200 else "")

    payload = {
        "touser":      openid,
        "template_id": config.WECHAT_TEMPLATE_ID,
        "data": {
            "heading": {"value": title[:32], "color": "#6C5CE7"},
            "body":    {"value": preview,    "color": "#444444"},
        },
    }
    return _post_json(
        f"{_WX_SEND_URL}?access_token={token}",
        payload,
        label="WeChat OA",
    )


# ── WeCom webhook ─────────────────────────────────────────────────────────────

def push_wecom(content: str, webhook_url: Optional[str] = None) -> bool:
    url = webhook_url or config.WECOM_WEBHOOK
    if not url:
        logger.error("WeCom webhook URL not configured.")
        return False
    payload = {"msgtype": "text", "text": {"content": content}}
    return _post_json(url, payload, label="WeCom")


def push_wecom_markdown(content: str, webhook_url: Optional[str] = None) -> bool:
    url = webhook_url or config.WECOM_WEBHOOK
    if not url:
        return False
    payload = {"msgtype": "markdown", "markdown": {"content": content}}
    return _post_json(url, payload, label="WeCom-markdown")


# ── Unified dispatcher ────────────────────────────────────────────────────────

def push(
    title: str,
    body: str,
    method: Optional[str] = None,
    target: Optional[str] = None,
) -> bool:
    """
    Route a push to the correct channel.

    Args:
        title:   Notification title.
        body:    Full message body (plain text / light markdown).
        method:  'serverchan' | 'email' | 'wechat_oa' | 'wecom'.
                 Defaults to config.PUSH_MODE (single-user mode).
        target:  Destination address/key/openid.
                 Defaults to the key in config (single-user mode).
    """
    m = (method or config.PUSH_MODE).lower()

    if m == "serverchan":
        return push_serverchan(title=title, content=body, key=target)

    if m == "email":
        if not target:
            logger.error("email push requires a target address.")
            return False
        return push_email(title=title, content=body, to_address=target)

    if m == "wechat_oa":
        if not target:
            logger.error("wechat_oa push requires an openid target.")
            return False
        return push_wechat_oa(title=title, content=body, openid=target)

    if m == "wecom":
        if any(c in body for c in ("**", "##", "- ")):
            return push_wecom_markdown(body, webhook_url=target or config.WECOM_WEBHOOK)
        return push_wecom(body, webhook_url=target or config.WECOM_WEBHOOK)

    logger.error("Unknown push method: %r", m)
    return False
