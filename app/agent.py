"""The bot: one call per incoming message. Model + tools + code guardrails + human handoff."""

from collections.abc import Callable
from datetime import date, datetime
from zoneinfo import ZoneInfo

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.business import Business
from app.db import Database
from app.guardrails import unverified_amounts, wants_human
from app.tools import TOOLS, Context

# Fixed replies sent by code, not by the model. Edit them for your business.
TEXTS = {
    "handoff": "Listo, te paso con una persona del equipo. Te responde por acá en breve 🙌",
    "fallback": "Perdón, no pude responder bien eso. Te paso con una persona del equipo para que te ayude.",
}

SYSTEM_PROMPT = """You are the WhatsApp assistant of {name}, a car rental company. Today is {today} ({weekday}).

How to work:
- Use the tools for every fact: cars, availability, prices, rules, bookings. If a tool doesn't give you \
the answer, say you don't know and offer to pass the chat to a person.
- Never calculate or estimate prices. Only repeat amounts exactly as a tool returned them.
- To book: search cars, send the quote from get_quote, ask for the driver's full name, and only call \
create_booking after the customer clearly accepts. A new booking is a REQUEST pending confirmation by the \
team; never say it is confirmed.
- Convert relative dates ("next Friday", "this weekend") into YYYY-MM-DD using today's date, and \
repeat the dates back so the customer can check them.
- Use handoff_to_human for complaints, accidents or damage, payments, changes or cancellations, special \
requests, or when you are unsure.

Style: reply in the customer's language. This is WhatsApp: short messages, plain text, at most a few \
lines, *single asterisks* for bold, no headings or tables. Be warm and direct."""


class Bot:
    def __init__(
        self,
        model: str | BaseChatModel,
        business: Business,
        db: Database,
        notify: Callable[[str, str], None] | None = None,
        history_limit: int = 20,
    ):
        self.business = business
        self.db = db
        self.notify = notify
        self.history_limit = history_limit
        self.agent = create_agent(model, tools=TOOLS, context_schema=Context)
        self.tz = ZoneInfo(business.data.get("timezone", "UTC"))

    def today(self) -> date:
        return datetime.now(self.tz).date()

    def handle(self, wa_id: str, text: str, name: str | None = None) -> str | None:
        """Process one customer message. Returns the reply to send, or None if a human has the chat."""
        conversation = self.db.get_conversation(wa_id, name)
        self.db.add_message(wa_id, "user", text)

        if conversation["status"] == "human":
            return None
        if wants_human(text):
            return self._handoff(wa_id, "Customer asked for a person")

        ctx = Context(wa_id=wa_id, today=self.today(), business=self.business, db=self.db)
        history = self.db.history(wa_id, self.history_limit)
        messages = [HumanMessage(m["content"]) if m["role"] == "user" else AIMessage(m["content"]) for m in history]
        previous_replies = [m["content"] for m in history if m["role"] == "assistant"]

        reply, tool_outputs = self._run(messages, ctx)
        bad = unverified_amounts(reply, tool_outputs + previous_replies)
        if bad:
            # One retry with a correction. If the model still invents a number, a person takes over.
            self.db.log_event(wa_id, "guardrail_blocked", f"{bad} in: {reply[:300]}")
            correction = HumanMessage(
                f"[System check, not from the customer] Your reply had amounts no tool returned: {bad}. "
                "Call get_quote and only repeat its exact numbers. Write the reply again."
            )
            reply, more = self._run(messages + [AIMessage(reply), correction], ctx)
            if unverified_amounts(reply, tool_outputs + more + previous_replies):
                self.db.log_event(wa_id, "guardrail_blocked", f"retry failed: {reply[:300]}")
                return self._handoff(wa_id, "The assistant could not give a verified price", TEXTS["fallback"])

        if ctx.handoff_reason:
            return self._handoff(wa_id, ctx.handoff_reason, reply or TEXTS["handoff"])

        reply = reply or TEXTS["fallback"]
        self.db.add_message(wa_id, "assistant", reply)
        return reply

    def _run(self, messages: list, ctx: Context) -> tuple[str, list[str]]:
        system = SYSTEM_PROMPT.format(
            name=self.business.data["name"], today=ctx.today.isoformat(), weekday=ctx.today.strftime("%A")
        )
        result = self.agent.invoke({"messages": [("system", system), *messages]}, context=ctx)
        new = result["messages"][len(messages) + 1 :]
        tool_outputs = [m.text for m in new if isinstance(m, ToolMessage)]
        final = next((m for m in reversed(new) if isinstance(m, AIMessage) and m.text.strip()), None)
        return (final.text.strip() if final else ""), tool_outputs

    def _handoff(self, wa_id: str, reason: str, reply: str = TEXTS["handoff"]) -> str:
        self.db.set_status(wa_id, "human", reason)
        self.db.log_event(wa_id, "handoff", reason)
        self.db.add_message(wa_id, "assistant", reply)
        if self.notify:
            self.notify(wa_id, reason)
        return reply
