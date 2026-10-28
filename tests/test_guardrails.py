from app.guardrails import unverified_amounts, wants_human


def test_amounts_from_tools_pass():
    tool = "TOTAL: $ 177.000\nSecurity deposit: $ 250.000"
    assert unverified_amounts("Te queda en *$177.000* más $ 250.000 de depósito", [tool]) == []


def test_invented_amount_is_caught():
    tool = "TOTAL: $ 177.000"
    assert unverified_amounts("Serían unos $ 180.000", [tool]) == ["$ 180.000"]
    assert unverified_amounts("Son 200.000 pesos", [tool]) == ["200.000 pesos"]


def test_text_without_money_passes():
    assert unverified_amounts("Tenemos 3 autos para el 12/01, ¿cuál te interesa?", []) == []


def test_wants_human():
    assert wants_human("quiero hablar con una persona")
    assert wants_human("Me pasás con un asesor?")
    assert wants_human("can I talk to someone")
    assert not wants_human("hola, quiero alquilar un auto")
