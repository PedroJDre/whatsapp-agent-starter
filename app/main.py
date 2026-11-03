"""FastAPI app: WhatsApp webhook, team dashboard and handoff release."""

import logging
import secrets
from html import escape

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from app.agent import Bot
from app.business import Business, money
from app.config import Settings, get_settings
from app.db import Database
from app.whatsapp import Incoming, WhatsAppClient, parse, valid_signature

log = logging.getLogger("bot")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

NOT_TEXT = "Por ahora solo puedo leer mensajes de texto. ¿Me lo escribís? 🙂"


def create_app(
    settings: Settings | None = None, bot: Bot | None = None, whatsapp: WhatsAppClient | None = None
) -> FastAPI:
    settings = settings or get_settings()
    whatsapp = whatsapp or WhatsAppClient(
        settings.whatsapp_token, settings.whatsapp_phone_number_id, settings.graph_api_version
    )

    def notify_team(wa_id: str, reason: str) -> None:
        if settings.human_notify_number:
            whatsapp.send_text(settings.human_notify_number, f"🙋 Chat con +{wa_id} pasado a una persona: {reason}")

    if bot is None:
        db = Database(settings.database_path)
        bot = Bot(
            settings.model,
            Business.from_file(settings.business_file, db),
            db,
            notify=notify_team,
            history_limit=settings.history_limit,
        )
    db = bot.db
    if not settings.whatsapp_app_secret:
        log.warning("WHATSAPP_APP_SECRET is empty: webhook signatures are NOT checked. Only OK for local tests.")

    app = FastAPI(title="WhatsApp Agent Starter")

    def check_admin(token: str) -> None:
        if not secrets.compare_digest(token.encode(), settings.admin_token.encode()):
            raise HTTPException(status_code=401, detail="Invalid token")

    def process(msg: Incoming) -> None:
        try:
            whatsapp.mark_read(msg.message_id)
        except Exception:  # blue ticks are nice to have; never skip the answer for them
            log.warning("Could not mark message %s as read", msg.message_id, exc_info=True)
        try:
            if not msg.text.strip():
                db.get_conversation(msg.wa_id, msg.name)
                db.add_message(msg.wa_id, "user", f"[{msg.type} message]")
                if db.get_conversation(msg.wa_id)["status"] == "bot":
                    db.add_message(msg.wa_id, "assistant", NOT_TEXT)
                    whatsapp.send_text(msg.wa_id, NOT_TEXT)
                return
            reply = bot.handle(msg.wa_id, msg.text, msg.name)
            if reply:
                whatsapp.send_text(msg.wa_id, reply)
        except Exception as exc:
            # Meta already got its 200 and the id is marked processed, so nothing will retry this message.
            # End in a handoff, not silence: a person takes the chat and the customer gets the fallback text.
            log.exception("Failed to process message %s", msg.message_id)
            db.log_event(msg.wa_id, "error", f"message {msg.message_id}: {type(exc).__name__}")
            try:
                db.get_conversation(msg.wa_id, msg.name)
                fallback = bot.fail_over(msg.wa_id, f"Error while answering ({type(exc).__name__})")
                if fallback:
                    whatsapp.send_text(msg.wa_id, fallback)
            except Exception:
                log.exception("Fallback for message %s also failed; the chat is waiting for a person", msg.message_id)

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/webhook")
    def verify(
        mode: str = Query("", alias="hub.mode"),
        token: str = Query("", alias="hub.verify_token"),
        challenge: str = Query("", alias="hub.challenge"),
    ):
        if mode == "subscribe" and secrets.compare_digest(token, settings.whatsapp_verify_token):
            return PlainTextResponse(challenge)
        raise HTTPException(status_code=403, detail="Verification failed")

    @app.post("/webhook")
    async def webhook(request: Request, background: BackgroundTasks):
        body = await request.body()
        if settings.whatsapp_app_secret and not valid_signature(
            body, request.headers.get("X-Hub-Signature-256"), settings.whatsapp_app_secret
        ):
            raise HTTPException(status_code=401, detail="Bad signature")
        try:
            payload = await request.json()
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid JSON") from None
        for msg in parse(payload):
            if db.mark_processed(msg.message_id):  # Meta retries webhooks; handle each message once
                background.add_task(process, msg)
        # Answer fast: Meta expects a 200 within a few seconds or it retries.
        return {"ok": True}

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard(token: str = ""):
        check_admin(token)
        return render_dashboard(db, bot.business, token)

    @app.post("/admin/release")
    def release(wa_id: str = Form(...), token: str = Form(...)):
        check_admin(token)
        db.set_status(wa_id, "bot")
        db.log_event(wa_id, "released", "Chat returned to the bot")
        return RedirectResponse(f"/dashboard?token={token}", status_code=303)

    return app


def render_dashboard(db: Database, business: Business, token: str) -> str:
    s = db.stats()
    tiles = "".join(
        f"<div class=tile><b>{s[k]}</b><span>{label}</span></div>"
        for k, label in [
            ("conversations", "Conversations"),
            ("messages", "Customer messages"),
            ("bookings", "Booking requests"),
            ("handoffs", "Handoffs to a person"),
            ("blocked_replies", "Replies blocked by guardrails"),
        ]
    )
    waiting = (
        "".join(
            f"<tr><td>+{escape(c['wa_id'])}</td><td>{escape(c['name'] or '')}</td><td>{escape(c['handoff_reason'] or '')}</td>"
            f"<td>{escape(c['updated_at'])}</td><td><form method=post action=/admin/release>"
            f"<input type=hidden name=wa_id value='{escape(c['wa_id'])}'><input type=hidden name=token value='{escape(token)}'>"
            f"<button>Return to bot</button></form></td></tr>"
            for c in db.conversations_by_status("human")
        )
        or "<tr><td colspan=5 class=muted>Nobody is waiting for a person.</td></tr>"
    )
    bookings = (
        "".join(
            f"<tr><td>{escape(b['code'])}</td><td>{escape(business.cars.get(b['car_id'], {}).get('name', b['car_id']))}</td>"
            f"<td>{escape(b['start_date'])} → {escape(b['end_date'])}</td><td>{escape(b['customer_name'])}</td>"
            f"<td>{money(b['total'])}</td><td>{escape(b['status'])}</td></tr>"
            for b in db.recent_bookings()
        )
        or "<tr><td colspan=6 class=muted>No bookings yet.</td></tr>"
    )
    tools = (
        "".join(f"<tr><td>{escape(t['detail'])}</td><td>{t['n']}</td></tr>" for t in s["tool_calls"])
        or "<tr><td colspan=2 class=muted>No tool calls yet.</td></tr>"
    )
    events = (
        "".join(
            f"<tr><td>{escape(e['created_at'])}</td><td>{escape(e['type'])}</td><td>+{escape(e['wa_id'] or '')}</td>"
            f"<td>{escape((e['detail'] or '')[:160])}</td></tr>"
            for e in s["recent_events"]
        )
        or "<tr><td colspan=4 class=muted>No events yet.</td></tr>"
    )
    return f"""<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>{escape(business.data["name"])} · Bot dashboard</title><style>
body{{font-family:system-ui,sans-serif;margin:0;padding:24px 16px;background:#f6f7f9;color:#1b1f24}}
main{{max-width:1000px;margin:auto}} h1{{font-size:22px}} h2{{font-size:16px;margin-top:32px}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}}
.tile{{background:#fff;border:1px solid #e3e6ea;border-radius:10px;padding:14px}}
.tile b{{display:block;font-size:26px}} .tile span{{color:#5b6470;font-size:13px}}
.wrap{{overflow-x:auto;background:#fff;border:1px solid #e3e6ea;border-radius:10px}}
table{{width:100%;border-collapse:collapse;font-size:14px}} td{{padding:9px 12px;border-top:1px solid #eef0f2}}
tr:first-child td{{border-top:0}} .muted{{color:#8a929c}}
button{{background:#1f6feb;color:#fff;border:0;border-radius:6px;padding:6px 10px;cursor:pointer}}
</style></head><body><main><h1>{escape(business.data["name"])} · Bot dashboard</h1>
<div class=tiles>{tiles}</div>
<h2>Waiting for a person</h2><div class=wrap><table>{waiting}</table></div>
<h2>Booking requests</h2><div class=wrap><table>{bookings}</table></div>
<h2>Tools used</h2><div class=wrap><table>{tools}</table></div>
<h2>Recent events</h2><div class=wrap><table>{events}</table></div>
</main></body></html>"""
