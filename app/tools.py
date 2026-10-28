"""LangChain tools. Each one is a thin wrapper around business.py; rule errors go back to the model as text."""

from dataclasses import dataclass
from datetime import date

from langchain.tools import ToolRuntime, tool

from app.business import Business, RuleError, money
from app.db import Database


@dataclass
class Context:
    """Per-message context injected into every tool call. Never visible to the model."""

    wa_id: str
    today: date
    business: Business
    db: Database
    handoff_reason: str | None = None


def _log(runtime: ToolRuntime[Context], name: str) -> None:
    runtime.context.db.log_event(runtime.context.wa_id, "tool_call", name)


@tool
def business_info(runtime: ToolRuntime[Context]) -> str:
    """Hours, branches, rental requirements, extras and their prices, discounts and the security deposit."""
    _log(runtime, "business_info")
    return runtime.context.business.info()


@tool
def search_cars(
    start_date: str,
    end_date: str,
    runtime: ToolRuntime[Context],
    category: str | None = None,
    min_seats: int = 1,
    transmission: str | None = None,
) -> str:
    """List cars available for the dates (YYYY-MM-DD, end = return date).

    category: economy, compact, suv or van. transmission: manual or automatic.
    """
    _log(runtime, "search_cars")
    ctx = runtime.context
    try:
        cars = ctx.business.search(start_date, end_date, ctx.today, category, min_seats, transmission)
    except RuleError as exc:
        return f"Can't search: {exc}"
    if not cars:
        return "No cars match those filters for those dates."
    return "\n".join(
        f"- id={c['id']}: {c['name']} ({c['category']}, {c['seats']} seats, {c['transmission']}), "
        f"{money(c['price_per_day'])} per day"
        for c in cars
    )


@tool
def get_quote(
    car_id: str, start_date: str, end_date: str, runtime: ToolRuntime[Context], extras: list[str] | None = None
) -> str:
    """Exact price for one car and dates. extras: child_seat, additional_driver, gps.

    Always use this before telling the customer any total. Never add up prices yourself.
    """
    _log(runtime, "get_quote")
    ctx = runtime.context
    try:
        quote = ctx.business.quote(car_id, start_date, end_date, ctx.today, extras)
    except RuleError as exc:
        return f"Can't quote: {exc}"
    available = ctx.business.available_units(car_id, quote.start, quote.end) > 0
    return quote.as_text(ctx.business) + ("" if available else "\nNOT AVAILABLE for these dates.")


@tool
def create_booking(
    car_id: str,
    start_date: str,
    end_date: str,
    customer_name: str,
    runtime: ToolRuntime[Context],
    extras: list[str] | None = None,
) -> str:
    """Create a booking request. Only call it after the customer saw the quote and clearly said yes.

    customer_name is the driver's full name.
    """
    _log(runtime, "create_booking")
    ctx = runtime.context
    try:
        code, quote = ctx.business.book(ctx.wa_id, car_id, start_date, end_date, customer_name, ctx.today, extras)
    except RuleError as exc:
        return f"Booking not created: {exc}"
    ctx.db.log_event(ctx.wa_id, "booking", f"{code} {car_id} {quote.start}→{quote.end} {money(quote.total)}")
    return (
        f"Booking request {code} created with status PENDING CONFIRMATION.\n{quote.as_text(ctx.business)}\n"
        "A person from the team confirms it within business hours. Do not tell the customer it is confirmed."
    )


@tool
def lookup_booking(code: str, runtime: ToolRuntime[Context]) -> str:
    """Status of a booking by its code (for example RT-1A2B3C)."""
    _log(runtime, "lookup_booking")
    ctx = runtime.context
    booking = ctx.db.get_booking(code.strip())
    # Privacy rule in code: a customer only sees bookings made from their own number.
    if booking is None or booking["wa_id"] != ctx.wa_id:
        return "No booking with that code was found for this phone number."
    car = ctx.business.cars.get(booking["car_id"], {"name": booking["car_id"]})
    return (
        f"Booking {booking['code']}: {car['name']}, {booking['start_date']} to {booking['end_date']}, "
        f"driver {booking['customer_name']}, total {money(booking['total'])}, status {booking['status']}."
    )


@tool
def handoff_to_human(reason: str, runtime: ToolRuntime[Context]) -> str:
    """Pass the chat to a person from the team. Use it for complaints, damage or accidents, payments,
    changes or cancellations of existing bookings, special requests, or whenever you are not sure.
    """
    _log(runtime, "handoff_to_human")
    runtime.context.handoff_reason = reason
    return "Handoff registered. Tell the customer a person from the team will continue the chat shortly."


TOOLS = [business_info, search_cars, get_quote, create_booking, lookup_booking, handoff_to_human]
