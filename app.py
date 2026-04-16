"""
app.py - Flask web application for the Daily Brief subscription service.

Routes
------
GET  /                       subscription landing page
POST /subscribe              save subscriber
GET  /success                confirmation page
GET  /unsubscribe?token=...  one-click unsubscribe

WeChat Official Account OAuth (for "scan QR, auto-subscribe" flow)
GET  /wechat/auth            redirect to WeChat OAuth
GET  /wechat/callback        WeChat redirects here with ?code=...
GET  /wechat/webhook         WeChat event verification (GET) + follow events (POST)

Admin (protected by X-Admin-Secret header or ?secret= query param)
GET  /admin/stats
POST /admin/run
GET  /health
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
import xml.etree.ElementTree as ET
from urllib.parse import quote, urlencode

import requests
from flask import (
    Flask, flash, jsonify, redirect, render_template,
    request, url_for, session,
)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import config
import database as db

logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", os.urandom(32))

ADMIN_SECRET: str = os.getenv("ADMIN_SECRET", "")

with app.app_context():
    db.init_db()


# ── Public routes ─────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template(
        "index.html",
        topics=db.TOPICS,
        topic_categories=db.TOPIC_CATEGORIES,
        delivery_methods=db.DELIVERY_METHODS,
        subscriber_count=db.subscriber_count(),
        wechat_oa_enabled=bool(config.WECHAT_APPID),
    )


@app.route("/subscribe", methods=["POST"])
def subscribe():
    delivery_method = request.form.get("delivery_method", "email").strip()
    delivery_target = request.form.get("delivery_target", "").strip()
    selected_topics = request.form.getlist("topics")
    name            = request.form.get("name", "").strip()

    if not delivery_target:
        flash("Please fill in your delivery address.", "error")
        return redirect(url_for("index"))

    if not selected_topics:
        flash("Please select at least one topic.", "error")
        return redirect(url_for("index"))

    try:
        sub = db.add_subscriber(
            delivery_method=delivery_method,
            delivery_target=delivery_target,
            topics=selected_topics,
            name=name,
        )
        return render_template(
            "success.html",
            name=name or "there",
            delivery_method=delivery_method,
            delivery_label=db.DELIVERY_METHODS.get(delivery_method, {}).get("label", delivery_method),
            topics=[db.TOPICS[t] for t in sub.topics if t in db.TOPICS],
            unsubscribe_token=sub.unsubscribe_token,
        )
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("index"))
    except Exception as exc:
        logger.error("Subscribe error: %s", exc)
        flash("Something went wrong — please try again.", "error")
        return redirect(url_for("index"))


@app.route("/unsubscribe")
def unsubscribe():
    token = request.args.get("token", "").strip()
    if not token:
        return render_template("unsubscribe.html", status="invalid")
    found = db.deactivate_by_token(token)
    return render_template("unsubscribe.html", status="success" if found else "not_found")


@app.route("/health")
def health():
    return jsonify({"status": "ok", "subscribers": db.subscriber_count()})


# ── WeChat Official Account - OAuth subscription flow ─────────────────────────
#
# How it works for the user:
#   1. Operator shares a link like https://yourdomain.com/wechat/auth
#   2. User opens it *inside WeChat browser*
#   3. WeChat silently exchanges a code for the user's openid (snsapi_base scope)
#   4. User sees the topic-selection form — no passwords, no SCT keys
#   5. On submit, we store their openid and send briefs automatically
#
# Requirements:
#   - WECHAT_APPID + WECHAT_APPSECRET in .env
#   - APP_BASE_URL must be registered as an OAuth redirect domain in WeChat MP backend
# ────────────────────────────────────────────────────────────────────────────────

_WX_OAUTH_URL = "https://open.weixin.qq.com/connect/oauth2/authorize"
_WX_TOKEN_URL = "https://api.weixin.qq.com/sns/oauth2/access_token"
_WX_USERINFO_URL = "https://api.weixin.qq.com/sns/userinfo"


@app.route("/wechat/auth")
def wechat_auth():
    """Redirect the user to WeChat OAuth. Works only inside WeChat browser."""
    if not config.WECHAT_APPID:
        return "WeChat Official Account is not configured.", 503

    callback = config.APP_BASE_URL.rstrip("/") + url_for("wechat_callback")
    params = {
        "appid":         config.WECHAT_APPID,
        "redirect_uri":  callback,
        "response_type": "code",
        "scope":         "snsapi_userinfo",   # gets nickname + avatar too
        "state":         "subscribe",
    }
    redirect_url = _WX_OAUTH_URL + "?" + urlencode(params) + "#wechat_redirect"
    return redirect(redirect_url)


@app.route("/wechat/callback")
def wechat_callback():
    """
    WeChat redirects here after user consent.
    Exchange the code for openid, then show the subscription form.
    """
    code  = request.args.get("code", "")
    state = request.args.get("state", "")

    if not code:
        return render_template("wechat_error.html",
                               msg="Authorization failed — please try again."), 400

    # Exchange code for access_token + openid
    try:
        resp = requests.get(_WX_TOKEN_URL, params={
            "appid":      config.WECHAT_APPID,
            "secret":     config.WECHAT_APPSECRET,
            "code":       code,
            "grant_type": "authorization_code",
        }, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.error("WeChat OAuth token exchange failed: %s", exc)
        return render_template("wechat_error.html", msg="Could not verify with WeChat."), 500

    openid = data.get("openid", "")
    if not openid:
        logger.error("No openid in WeChat response: %s", data)
        return render_template("wechat_error.html", msg="Could not get your WeChat ID."), 500

    # Try to get nickname (requires snsapi_userinfo scope)
    nickname = ""
    access_token = data.get("access_token", "")
    if access_token:
        try:
            ui = requests.get(_WX_USERINFO_URL, params={
                "access_token": access_token,
                "openid": openid,
                "lang": "zh_CN",
            }, timeout=8).json()
            nickname = ui.get("nickname", "")
        except Exception:
            pass

    # Check if already subscribed
    existing = db.get_subscriber_by_openid(openid)
    if existing and existing.active:
        return render_template(
            "wechat_already.html",
            name=existing.name or nickname or "there",
            topics=[db.TOPICS[t] for t in existing.topics if t in db.TOPICS],
            unsubscribe_token=existing.unsubscribe_token,
        )

    # Show topic-selection form with openid pre-filled (hidden field)
    return render_template(
        "wechat_subscribe.html",
        openid=openid,
        nickname=nickname,
        topics=db.TOPICS,
        topic_categories=db.TOPIC_CATEGORIES,
    )


# ── WeChat Official Account - server-side event webhook ──────────────────────

@app.route("/wechat/webhook", methods=["GET", "POST"])
def wechat_webhook():
    """
    WeChat server verification (GET) and event handler (POST).

    Configure this URL in the WeChat MP backend under "Server Configuration".
    Token must match WECHAT_WEBHOOK_TOKEN in your .env.
    """
    token = os.getenv("WECHAT_WEBHOOK_TOKEN", "dailybrief")

    if request.method == "GET":
        # WeChat sends echostr to verify the endpoint
        signature = request.args.get("signature", "")
        timestamp = request.args.get("timestamp", "")
        nonce     = request.args.get("nonce", "")
        echostr   = request.args.get("echostr", "")

        check = hashlib.sha1(
            "".join(sorted([token, timestamp, nonce])).encode()
        ).hexdigest()

        if check == signature:
            return echostr, 200
        return "Forbidden", 403

    # POST: handle follow / unfollow events
    try:
        xml_data = ET.fromstring(request.data)
        msg_type = xml_data.findtext("MsgType", "")
        event    = xml_data.findtext("Event", "")
        openid   = xml_data.findtext("FromUserName", "")

        if msg_type == "event" and event == "unsubscribe" and openid:
            existing = db.get_subscriber_by_openid(openid)
            if existing:
                db.deactivate_by_token(existing.unsubscribe_token)
                logger.info("User unfollowed OA, deactivated subscriber openid=%.12s", openid)

    except Exception as exc:
        logger.warning("WeChat webhook parse error: %s", exc)

    return "<xml><return_code>SUCCESS</return_code></xml>", 200, \
           {"Content-Type": "application/xml"}


# ── Admin routes ──────────────────────────────────────────────────────────────

def _require_admin():
    if not ADMIN_SECRET:
        from flask import abort
        abort(403, "ADMIN_SECRET is not configured.")
    provided = request.headers.get("X-Admin-Secret") or request.args.get("secret")
    if provided != ADMIN_SECRET:
        from flask import abort
        abort(401, "Unauthorized.")


@app.route("/admin/stats")
def admin_stats():
    _require_admin()
    subs = db.get_active_subscribers()
    method_breakdown = {}
    for s in subs:
        method_breakdown[s.delivery_method] = method_breakdown.get(s.delivery_method, 0) + 1
    topic_breakdown = {
        tid: sum(1 for s in subs if tid in s.topics)
        for tid in db.TOPICS
    }
    return jsonify({
        "active_subscribers": len(subs),
        "by_delivery_method": method_breakdown,
        "by_topic": topic_breakdown,
    })


@app.route("/admin/run", methods=["POST"])
def admin_run():
    _require_admin()
    try:
        from main import run_daily_for_subscribers
        sent, failed = run_daily_for_subscribers()
        return jsonify({"sent": sent, "failed": failed})
    except Exception as exc:
        logger.error("Admin run failed: %s", exc)
        return jsonify({"error": str(exc)}), 500


# ── Dev entry ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port  = int(os.getenv("PORT", 5000))
    debug = os.getenv("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
