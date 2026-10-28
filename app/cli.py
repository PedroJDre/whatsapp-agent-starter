"""Chat with the bot in your terminal, no WhatsApp needed: uv run python -m app.cli"""

import argparse
import sys

from app.agent import Bot
from app.business import Business
from app.config import get_settings
from app.db import Database


def main() -> None:
    parser = argparse.ArgumentParser(description="Chat with the bot in the terminal.")
    parser.add_argument("--phone", default="5491100000000", help="Fake customer phone number")
    parser.add_argument("--name", default="Demo")
    args = parser.parse_args()

    settings = get_settings()
    if settings.model.startswith("anthropic:") and not settings.anthropic_api_key:
        sys.exit("Set ANTHROPIC_API_KEY in .env first (or change MODEL to another provider).")

    db = Database(settings.database_path)
    business = Business.from_file(settings.business_file, db)
    bot = Bot(
        settings.model,
        business,
        db,
        history_limit=settings.history_limit,
        notify=lambda wa_id, reason: print(f"  [team notified: {reason}]"),
    )

    if db.get_conversation(args.phone)["status"] == "human":
        db.set_status(args.phone, "bot")  # start every terminal session with the bot in charge
    print(f"Chatting with {business.data['name']} as +{args.phone}. Ctrl+C to quit.\n")
    while True:
        try:
            text = input("you > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not text:
            continue
        reply = bot.handle(args.phone, text, args.name)
        if reply is None:
            print("  [a person has this chat; the bot stays quiet. Restart to reset.]\n")
        else:
            print(f"bot > {reply}\n")


if __name__ == "__main__":
    main()
