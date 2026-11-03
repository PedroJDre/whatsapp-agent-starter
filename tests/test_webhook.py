import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app.agent import TEXTS
from app.config import Settings
from app.main import create_app
from tests.conftest import ai

SECRET = "app-secret"


class FakeWhatsApp:
    def __init__(self):
        self.sent = []

    def send_text(self, to, text):
        self.sent.append((to, text))

    def mark_read(self, message_id):
        pass


def payload(text="hola", msg_id="wamid.1", kind="text"):
    msg = {"from": "5491122223333", "id": msg_id, "type": kind}
    if kind == "text":
        msg["text"] = {"body": text}
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [{"wa_id": "5491122223333", "profile": {"name": "Ana"}}],
                            "messages": [msg],
                        }
                    }
                ]
            }
        ]
    }


def signed(body: dict) -> tuple[bytes, dict]:
    raw = json.dumps(body).encode()
    sig = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {"X-Hub-Signature-256": sig, "Content-Type": "application/json"}


@pytest.fixture
def setup(make_bot):
    def factory(*script):
        wa = FakeWhatsApp()
        settings = Settings(whatsapp_app_secret=SECRET, whatsapp_verify_token="verify", admin_token="admin")
        bot = make_bot(*script)
        return TestClient(create_app(settings, bot=bot, whatsapp=wa)), wa, bot

    return factory


def test_verification(setup):
    client, _, _ = setup()
    ok = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "verify", "hub.challenge": "42"})
    assert ok.status_code == 200 and ok.text == "42"
    bad = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "42"})
    assert bad.status_code == 403


def test_rejects_bad_signature(setup):
    client, wa, _ = setup()
    raw, headers = signed(payload())
    headers["X-Hub-Signature-256"] = "sha256=" + "0" * 64
    assert client.post("/webhook", content=raw, headers=headers).status_code == 401
    assert wa.sent == []


def test_replies_once_even_if_meta_retries(setup):
    client, wa, _ = setup(ai("¡Hola Ana! ¿Para qué fechas buscás auto?"))
    raw, headers = signed(payload())
    assert client.post("/webhook", content=raw, headers=headers).status_code == 200
    assert client.post("/webhook", content=raw, headers=headers).status_code == 200  # retry, same id
    assert wa.sent == [("5491122223333", "¡Hola Ana! ¿Para qué fechas buscás auto?")]


def test_non_text_message(setup):
    client, wa, _ = setup()
    raw, headers = signed(payload(kind="image", msg_id="wamid.img"))
    client.post("/webhook", content=raw, headers=headers)
    assert "texto" in wa.sent[0][1]


def test_dashboard_and_release(setup):
    client, wa, bot = setup()
    raw, headers = signed(payload("quiero hablar con un asesor"))
    client.post("/webhook", content=raw, headers=headers)
    assert bot.db.get_conversation("5491122223333")["status"] == "human"

    assert client.get("/dashboard", params={"token": "wrong"}).status_code == 401
    page = client.get("/dashboard", params={"token": "admin"})
    assert page.status_code == 200 and "Customer asked for a person" in page.text

    r = client.post("/admin/release", data={"wa_id": "5491122223333", "token": "admin"}, follow_redirects=False)
    assert r.status_code == 303
    assert bot.db.get_conversation("5491122223333")["status"] == "bot"


class FlakyWhatsApp(FakeWhatsApp):
    """The first send fails (like a Graph API 5xx), the next ones work."""

    def __init__(self):
        super().__init__()
        self.failures = 1

    def send_text(self, to, text):
        if self.failures:
            self.failures -= 1
            raise RuntimeError("Graph API 503")
        super().send_text(to, text)


def test_model_error_hands_off_instead_of_silence(setup):
    client, wa, bot = setup()

    def boom(*args, **kwargs):
        raise TimeoutError("model timed out")

    bot.agent.invoke = boom
    raw, headers = signed(payload("hola, ¿tienen autos?"))
    assert client.post("/webhook", content=raw, headers=headers).status_code == 200
    conv = bot.db.get_conversation("5491122223333")
    assert conv["status"] == "human" and "TimeoutError" in conv["handoff_reason"]
    assert wa.sent == [("5491122223333", TEXTS["fallback"])]


def test_send_error_hands_off_and_retries_with_fallback(make_bot):
    wa = FlakyWhatsApp()
    settings = Settings(whatsapp_app_secret=SECRET, whatsapp_verify_token="verify", admin_token="admin")
    bot = make_bot(ai("¡Hola Ana!"))
    client = TestClient(create_app(settings, bot=bot, whatsapp=wa))
    raw, headers = signed(payload())
    client.post("/webhook", content=raw, headers=headers)
    assert bot.db.get_conversation("5491122223333")["status"] == "human"
    assert wa.sent == [("5491122223333", TEXTS["fallback"])]


def test_error_when_a_person_already_has_the_chat_sends_nothing(setup):
    client, wa, bot = setup()
    bot.db.get_conversation("5491122223333", "Ana")
    bot.db.set_status("5491122223333", "human", "Customer asked for a person")
    bot.handle = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db locked"))
    raw, headers = signed(payload("hola"))
    client.post("/webhook", content=raw, headers=headers)
    assert wa.sent == []
