from datetime import date
from pathlib import Path

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from app.agent import Bot
from app.business import Business
from app.db import Database

BUSINESS_FILE = Path(__file__).resolve().parent.parent / "data" / "business.json"
TODAY = date(2030, 1, 7)  # a Monday


class ScriptedModel(GenericFakeChatModel):
    """Returns pre-written AI messages in order, including tool calls. No API key needed."""

    def bind_tools(self, tools, **kwargs):
        return self


def ai(text: str = "", *calls: tuple[str, dict]) -> AIMessage:
    return AIMessage(
        content=text,
        tool_calls=[{"name": name, "args": args, "id": f"call_{i}"} for i, (name, args) in enumerate(calls)],
    )


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "test.db")


@pytest.fixture
def business(db):
    return Business.from_file(BUSINESS_FILE, db)


@pytest.fixture
def make_bot(business, db):
    def factory(*script: AIMessage, notify=None) -> Bot:
        bot = Bot(ScriptedModel(messages=iter(script)), business, db, notify=notify)
        bot.today = lambda: TODAY
        return bot

    return factory
