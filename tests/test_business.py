import pytest

from app.business import RuleError, money
from tests.conftest import TODAY


def test_money_format():
    assert money(1250000) == "$ 1.250.000"


def test_quote_short_rental(business):
    q = business.quote("vw-polo", "2030-01-09", "2030-01-12", TODAY, ["gps"])
    assert q.days == 3
    assert q.total == 3 * 55000 + 3 * 4000
    assert q.discount == 0


def test_weekly_discount(business):
    q = business.quote("fiat-mobi", "2030-01-08", "2030-01-15", TODAY)
    assert q.days == 7
    assert q.discount == round(7 * 42000 * 0.10)
    assert q.total == 7 * 42000 - q.discount


@pytest.mark.parametrize(
    "start,end,error",
    [
        ("2030-01-01", "2030-01-03", "past"),
        ("2030-01-10", "2030-01-10", "after the pick-up"),
        ("2030-01-08", "2030-02-20", "maximum rental"),
        ("2030-01-13", "2030-01-15", "Sunday"),
        ("10/01/2030", "2030-01-15", "YYYY-MM-DD"),
        ("2031-01-08", "2031-01-10", "days ahead"),
    ],
)
def test_date_rules(business, start, end, error):
    with pytest.raises(RuleError, match=error):
        business.quote("vw-polo", start, end, TODAY)


def test_unknown_car_and_extra(business):
    with pytest.raises(RuleError, match="Unknown car"):
        business.quote("ferrari", "2030-01-08", "2030-01-10", TODAY)
    with pytest.raises(RuleError, match="Unknown extras"):
        business.quote("vw-polo", "2030-01-08", "2030-01-10", TODAY, ["jetpack"])


def test_availability_runs_out(business):
    # The van has a single unit.
    business.book("111", "toyota-hiace", "2030-01-08", "2030-01-12", "Ana Pérez", TODAY)
    with pytest.raises(RuleError, match="not available"):
        business.book("222", "toyota-hiace", "2030-01-10", "2030-01-14", "Juan Gómez", TODAY)
    # Back-to-back is fine: returned on the 12th, picked up on the 12th.
    business.book("222", "toyota-hiace", "2030-01-12", "2030-01-14", "Juan Gómez", TODAY)
    assert all(c["id"] != "toyota-hiace" for c in business.search("2030-01-09", "2030-01-11", TODAY))
