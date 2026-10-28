"""Business rules in plain code. The model never computes prices or decides availability."""

import json
import secrets
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from app.db import Database


class RuleError(ValueError):
    """A request that breaks a business rule. The message is shown to the model."""


def money(amount: int) -> str:
    """Argentine format: $ 1.234.567"""
    return "$ " + f"{amount:,}".replace(",", ".")


@dataclass
class Quote:
    car: dict
    start: date
    end: date
    days: int
    extras: list[str]
    base: int
    extras_total: int
    discount: int
    total: int

    def as_text(self, business: "Business") -> str:
        lines = [
            f"{self.car['name']} from {self.start.isoformat()} to {self.end.isoformat()} ({self.days} days)",
            f"- Rental: {self.days} x {money(self.car['price_per_day'])} = {money(self.base)}",
        ]
        for key in self.extras:
            extra = business.extras[key]
            lines.append(
                f"- {extra['name']}: {self.days} x {money(extra['price_per_day'])} = "
                f"{money(extra['price_per_day'] * self.days)}"
            )
        if self.discount:
            lines.append(f"- Weekly discount ({business.rules['weekly_discount_percent']}%): -{money(self.discount)}")
        lines.append(f"TOTAL: {money(self.total)}")
        lines.append(f"Security deposit (held on credit card, refunded): {money(business.rules['security_deposit'])}")
        return "\n".join(lines)


class Business:
    def __init__(self, data: dict, db: Database):
        self.data = data
        self.db = db
        self.rules = data["rules"]
        self.extras = data["extras"]
        self.cars = {car["id"]: car for car in data["cars"]}

    @classmethod
    def from_file(cls, path: Path, db: Database) -> "Business":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")), db)

    # --- info ----------------------------------------------------------
    def info(self) -> str:
        d = self.data
        branches = "\n".join(f"- {b['name']}: {b['address']}" for b in d["branches"])
        requirements = "\n".join(f"- {r}" for r in d["requirements"])
        extras = "\n".join(f"- {e['name']}: {money(e['price_per_day'])} per day" for e in self.extras.values())
        r = self.rules
        return (
            f"{d['name']}\nHours: {d['hours']}\nBranches:\n{branches}\nRequirements:\n{requirements}\n"
            f"Extras:\n{extras}\n"
            f"Rental length: {r['min_days']} to {r['max_days']} days. "
            f"{r['km_included_per_day']} km included per day. "
            f"{r['weekly_discount_percent']}% off rentals of 7 days or more. "
            f"Security deposit: {money(r['security_deposit'])}."
        )

    # --- validation ----------------------------------------------------
    def _parse_dates(self, start: str, end: str, today: date) -> tuple[date, date, int]:
        try:
            s, e = date.fromisoformat(start), date.fromisoformat(end)
        except ValueError as exc:
            raise RuleError("Dates must be in YYYY-MM-DD format.") from exc
        if s < today:
            raise RuleError(f"The pick-up date {s} is in the past. Today is {today}.")
        if (s - today).days > self.rules["max_days_ahead"]:
            raise RuleError(f"Bookings can be made up to {self.rules['max_days_ahead']} days ahead.")
        days = (e - s).days
        if days < self.rules["min_days"]:
            raise RuleError("The return date must be after the pick-up date.")
        if days > self.rules["max_days"]:
            raise RuleError(
                f"The maximum rental is {self.rules['max_days']} days. For longer rentals, "
                "a person from the team has to help."
            )
        if s.weekday() == 6 or e.weekday() == 6:
            raise RuleError("Branches are closed on Sundays, so pick-up and return can't be on a Sunday.")
        return s, e, days

    def _car(self, car_id: str) -> dict:
        if car_id not in self.cars:
            raise RuleError(f"Unknown car '{car_id}'. Valid ids: {', '.join(self.cars)}.")
        return self.cars[car_id]

    def _extras(self, extras: list[str] | None) -> list[str]:
        extras = extras or []
        unknown = [x for x in extras if x not in self.extras]
        if unknown:
            raise RuleError(f"Unknown extras {unknown}. Valid ones: {', '.join(self.extras)}.")
        return extras

    # --- operations ----------------------------------------------------
    def available_units(self, car_id: str, start: date, end: date) -> int:
        return self.cars[car_id]["units"] - self.db.overlapping_bookings(car_id, start, end)

    def search(
        self,
        start: str,
        end: str,
        today: date,
        category: str | None = None,
        min_seats: int = 1,
        transmission: str | None = None,
    ) -> list[dict]:
        s, e, days = self._parse_dates(start, end, today)
        results = []
        for car in self.cars.values():
            if category and car["category"] != category:
                continue
            if car["seats"] < min_seats or (transmission and car["transmission"] != transmission):
                continue
            if self.available_units(car["id"], s, e) > 0:
                results.append(car)
        return results

    def quote(self, car_id: str, start: str, end: str, today: date, extras: list[str] | None = None) -> Quote:
        car = self._car(car_id)
        s, e, days = self._parse_dates(start, end, today)
        extras = self._extras(extras)
        base = car["price_per_day"] * days
        extras_total = sum(self.extras[x]["price_per_day"] * days for x in extras)
        discount = 0
        if days >= 7:
            discount = round((base + extras_total) * self.rules["weekly_discount_percent"] / 100)
        return Quote(car, s, e, days, extras, base, extras_total, discount, base + extras_total - discount)

    def book(
        self,
        wa_id: str,
        car_id: str,
        start: str,
        end: str,
        customer_name: str,
        today: date,
        extras: list[str] | None = None,
    ) -> tuple[str, Quote]:
        if len(customer_name.strip()) < 3:
            raise RuleError("I need the driver's full name to make the booking.")
        quote = self.quote(car_id, start, end, today, extras)
        if self.available_units(car_id, quote.start, quote.end) <= 0:
            raise RuleError(f"The {quote.car['name']} is not available for those dates.")
        code = "RT-" + secrets.token_hex(3).upper()
        self.db.add_booking(
            code=code,
            wa_id=wa_id,
            car_id=car_id,
            start=quote.start,
            end=quote.end,
            extras=quote.extras,
            customer_name=customer_name.strip(),
            total=quote.total,
        )
        return code, quote
