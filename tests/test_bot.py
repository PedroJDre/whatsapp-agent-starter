from tests.conftest import TODAY, ai


def test_full_booking_flow(make_bot, db):
    bot = make_bot(
        ai("", ("get_quote", {"car_id": "vw-polo", "start_date": "2030-01-09", "end_date": "2030-01-12"})),
        ai("El Polo del 10 al 13 de enero te queda en $ 165.000. ¿Lo reservo? Pasame el nombre del conductor."),
        ai(
            "",
            (
                "create_booking",
                {
                    "car_id": "vw-polo",
                    "start_date": "2030-01-09",
                    "end_date": "2030-01-12",
                    "customer_name": "Ana Pérez",
                },
            ),
        ),
        ai("Listo Ana, tu solicitud quedó registrada. El equipo te la confirma en breve."),
    )
    first = bot.handle("5491111111111", "cuánto sale el polo del 10 al 13?")
    assert "$ 165.000" in first
    second = bot.handle("5491111111111", "sí, a nombre de Ana Pérez")
    assert "solicitud" in second
    [booking] = db.recent_bookings()
    assert booking["status"] == "pending_confirmation"
    assert booking["total"] == 165000
    assert booking["wa_id"] == "5491111111111"


def test_invented_price_is_blocked_then_corrected(make_bot, db):
    bot = make_bot(
        ai("Sale aprox $ 150.000"),  # made up, no tool call
        ai("", ("get_quote", {"car_id": "vw-polo", "start_date": "2030-01-09", "end_date": "2030-01-12"})),
        ai("Te queda en $ 165.000 por 3 días."),
    )
    reply = bot.handle("1", "precio polo 10 al 13")
    assert reply == "Te queda en $ 165.000 por 3 días."
    assert db.stats()["blocked_replies"] == 1


def test_repeated_invented_price_goes_to_a_person(make_bot, db):
    notified = []
    bot = make_bot(ai("Sale $ 150.000"), ai("Sale $ 151.000"), notify=lambda w, r: notified.append(w))
    reply = bot.handle("1", "precio polo")
    assert "persona" in reply and "151" not in reply
    assert db.get_conversation("1")["status"] == "human"
    assert notified == ["1"]


def test_asking_for_a_person_skips_the_model(make_bot, db):
    bot = make_bot()  # empty script: calling the model would fail
    reply = bot.handle("1", "quiero hablar con una persona")
    assert "persona" in reply
    assert db.get_conversation("1")["status"] == "human"
    assert bot.handle("1", "hola?") is None  # bot stays quiet while a person has the chat


def test_handoff_tool(make_bot, db):
    bot = make_bot(
        ai("", ("handoff_to_human", {"reason": "Car damage report"})),
        ai("Uh, lo siento. Te paso con alguien del equipo ya mismo."),
    )
    reply = bot.handle("1", "choqué el auto")
    assert "equipo" in reply
    conv = db.get_conversation("1")
    assert conv["status"] == "human" and conv["handoff_reason"] == "Car damage report"


def spy_tool_outputs(bot) -> list[str]:
    """Record what the tools returned to the model during bot.handle()."""
    outputs: list[str] = []
    original = bot._run

    def run(messages, ctx):
        reply, out = original(messages, ctx)
        outputs.extend(out)
        return reply, out

    bot._run = run
    return outputs


def test_lookup_only_shows_own_bookings(make_bot, business):
    code, _ = business.book("owner", "vw-polo", "2030-01-09", "2030-01-12", "Ana Pérez", TODAY)
    bot = make_bot(
        ai("", ("lookup_booking", {"code": code})),
        ai("No encontré esa reserva."),
        ai("", ("lookup_booking", {"code": code.lower()})),
        ai("Tu reserva está pendiente."),
    )
    outputs = spy_tool_outputs(bot)
    bot.handle("stranger", f"estado de {code}")
    bot.handle("owner", f"estado de {code}")
    assert "No booking" in outputs[0] and "Ana" not in outputs[0]
    assert "Ana Pérez" in outputs[1] and "pending_confirmation" in outputs[1]


def test_rule_errors_reach_the_model(make_bot):
    bot = make_bot(
        ai("", ("get_quote", {"car_id": "vw-polo", "start_date": "2030-01-13", "end_date": "2030-01-15"})),
        ai("Los domingos estamos cerrados, ¿te sirve el lunes 14?"),
    )
    outputs = spy_tool_outputs(bot)
    reply = bot.handle("1", "polo del domingo 13 al 15")
    assert "Sunday" in outputs[0]
    assert "domingos" in reply
