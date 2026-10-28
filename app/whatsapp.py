"""WhatsApp Cloud API: webhook signature check, payload parsing and sending messages."""

import hashlib
import hmac
import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)


@dataclass
class Incoming:
    message_id: str
    wa_id: str
    name: str | None
    type: str
    text: str


def valid_signature(body: bytes, header: str | None, app_secret: str) -> bool:
    """Meta signs every webhook with your app secret (X-Hub-Signature-256: sha256=<hex>)."""
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def parse(payload: dict) -> list[Incoming]:
    """Customer messages in a webhook payload. Delivery/read statuses are ignored."""
    messages = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            names = {c["wa_id"]: c.get("profile", {}).get("name") for c in value.get("contacts", [])}
            for msg in value.get("messages", []):
                kind = msg.get("type", "")
                if kind == "text":
                    text = msg["text"]["body"]
                elif kind == "button":
                    text = msg["button"].get("text", "")
                elif kind == "interactive":
                    reply = msg["interactive"].get("button_reply") or msg["interactive"].get("list_reply") or {}
                    text = reply.get("title", "")
                else:
                    text = ""
                messages.append(Incoming(msg["id"], msg["from"], names.get(msg["from"]), kind, text))
    return messages


class WhatsAppClient:
    def __init__(self, token: str, phone_number_id: str, api_version: str = "v23.0"):
        self.url = f"https://graph.facebook.com/{api_version}/{phone_number_id}/messages"
        self.headers = {"Authorization": f"Bearer {token}"}
        self.enabled = bool(token and phone_number_id)

    def send_text(self, to: str, text: str) -> None:
        if not self.enabled:
            log.warning("WhatsApp not configured; would send to %s: %s", to, text)
            return
        body = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text[:4096]}}
        response = httpx.post(self.url, json=body, headers=self.headers, timeout=20)
        if response.status_code >= 400:
            log.error("WhatsApp send failed (%s): %s", response.status_code, response.text)
        response.raise_for_status()

    def mark_read(self, message_id: str) -> None:
        if not self.enabled:
            return
        body = {"messaging_product": "whatsapp", "status": "read", "message_id": message_id}
        try:
            httpx.post(self.url, json=body, headers=self.headers, timeout=10)
        except httpx.HTTPError:
            log.warning("Could not mark message %s as read", message_id)
