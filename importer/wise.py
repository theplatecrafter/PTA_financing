# importer/wise.py

import csv
import hashlib
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from .common import ParsedRecord


def parse(path: Path) -> list[ParsedRecord]:
    """
    Parse a Wise transaction-history CSV into normalized ParsedRecord objects.

    This parser only reads and normalizes information supplied by Wise.

    It does not:
        - assign a Beancount account
        - decide what the transaction means
        - identify transfers between your own accounts
        - classify income/expenses
        - merge records
    """

    records: list[ParsedRecord] = []

    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)

        for row_number, row in enumerate(reader, start=2):
            records.append(
                parse_row(
                    row=row,
                    source_file=path,
                    source_row=row_number,
                )
            )

    return records


def parse_row(
    row: dict[str, str],
    source_file: Path,
    source_row: int,
) -> ParsedRecord:
    """
    Convert one Wise CSV row into a ParsedRecord.
    """

    source_id = clean(row.get("ID"))

    created_at = parse_datetime(row.get("Created on"))
    finished_at = parse_datetime(row.get("Finished on"))

    source_amount = parse_decimal(row.get("Source amount (after fees)"))
    source_currency = clean(row.get("Source currency"))

    target_amount = parse_decimal(row.get("Target amount (after fees)"))
    target_currency = clean(row.get("Target currency"))

    source_fee_amount = parse_decimal(row.get("Source fee amount"))
    source_fee_currency = clean(row.get("Source fee currency"))

    target_fee_amount = parse_decimal(row.get("Target fee amount"))
    target_fee_currency = clean(row.get("Target fee currency"))

    exchange_rate = parse_decimal(row.get("Exchange rate"))

    direction = clean(row.get("Direction"))

    # Wise rows can contain two monetary sides.
    # We do NOT try to decide which one is "the" transaction amount.
    amount, currency = determine_primary_amount(
        direction=direction,
        source_amount=source_amount,
        source_currency=source_currency,
        target_amount=target_amount,
        target_currency=target_currency,
    )

    transaction_date = (
        created_at.date()
        if created_at is not None
        else finished_at.date()
        if finished_at is not None
        else raise_missing_date(source_id, source_row)
    )

    transaction_time = (
        created_at.time()
        if created_at is not None
        else None
    )

    record_id = make_record_id(
        source_id=source_id,
        row=row,
    )

    raw_data = dict(row)

    return ParsedRecord(
        record_id=record_id,
        source="wise",
        source_file=source_file,
        source_row=source_row,
        source_id=source_id,

        date=transaction_date,
        time=transaction_time,
        completed_at=finished_at,

        description=clean(row.get("Category")),
        amount=amount,
        currency=currency,

        balance=None,
        balance_currency=None,

        counterparty=clean(row.get("Target name")),
        category=clean(row.get("Category")),
        payment_method=None,
        reference=clean(row.get("Reference")),
        note=clean(row.get("Note")),
        tags=[],

        payment_type=None,
        installment_number=None,
        payment_amount=None,

        source_amount=source_amount,
        source_currency=source_currency,

        target_amount=target_amount,
        target_currency=target_currency,

        exchange_rate=exchange_rate,
        conversion_date=None,

        fee_amount=determine_fee_amount(
            source_fee_amount=source_fee_amount,
            source_fee_currency=source_fee_currency,
            target_fee_amount=target_fee_amount,
            target_fee_currency=target_fee_currency,
        ),
        fee_currency=determine_fee_currency(
            source_fee_amount=source_fee_amount,
            source_fee_currency=source_fee_currency,
            target_fee_amount=target_fee_amount,
            target_fee_currency=target_fee_currency,
        ),

        raw_data=raw_data,
    )


def clean(value: str | None) -> str | None:
    """
    Strip whitespace and convert empty fields to None.
    """
    if value is None:
        return None

    value = value.strip()

    return value if value else None


def parse_datetime(value: str | None) -> datetime | None:
    """
    Parse Wise's datetime format.

    Example:
        2026-05-17 22:20:19
    """

    value = clean(value)

    if value is None:
        return None

    return datetime.strptime(
        value,
        "%Y-%m-%d %H:%M:%S",
    )


def parse_decimal(value: str | None) -> Decimal | None:
    """
    Parse a Wise decimal value.

    Examples:
        "436.79" -> Decimal("436.79")
        "459"    -> Decimal("459")
        ""       -> None
    """

    value = clean(value)

    if value is None:
        return None

    return Decimal(value)


def determine_primary_amount(
    direction: str | None,
    source_amount: Decimal | None,
    source_currency: str | None,
    target_amount: Decimal | None,
    target_currency: str | None,
) -> tuple[Decimal | None, str | None]:
    """
    Select a primary amount for the common ParsedRecord.

    This is ONLY a normalization convenience; it is not an
    accounting interpretation.

    For OUT:
        use the source side, if available.

    For IN:
        use the target side, if available.

    For NEUTRAL:
        prefer the source side.

    If the direction is unavailable, use the first available side.
    """

    if direction == "OUT":
        if source_amount is not None:
            return -abs(source_amount), source_currency

        if target_amount is not None:
            return target_amount, target_currency

    elif direction == "IN":
        if target_amount is not None:
            return abs(target_amount), target_currency

        if source_amount is not None:
            return source_amount, source_currency

    elif direction == "NEUTRAL":
        if source_amount is not None:
            return source_amount, source_currency

        if target_amount is not None:
            return target_amount, target_currency

    if source_amount is not None:
        return source_amount, source_currency

    if target_amount is not None:
        return target_amount, target_currency

    return None, None


def determine_fee_amount(
    source_fee_amount: Decimal | None,
    source_fee_currency: str | None,
    target_fee_amount: Decimal | None,
    target_fee_currency: str | None,
) -> Decimal | None:
    """
    Return a fee amount for the common schema.

    If both source and target fees exist, the source fee is used
    as the primary fee and the complete original values remain in
    raw_data.

    We should revisit this if real Wise files contain both sides
    simultaneously.
    """

    if source_fee_amount is not None:
        return source_fee_amount

    if target_fee_amount is not None:
        return target_fee_amount

    return None


def determine_fee_currency(
    source_fee_amount: Decimal | None,
    source_fee_currency: str | None,
    target_fee_amount: Decimal | None,
    target_fee_currency: str | None,
) -> str | None:

    if source_fee_amount is not None:
        return source_fee_currency

    if target_fee_amount is not None:
        return target_fee_currency

    return None


def make_record_id(
    source_id: str | None,
    row: dict[str, str],
) -> str:
    """
    Generate a deterministic identifier.

    Wise provides an ID, but we include the raw row as a fallback
    and as protection against unusual cases where an ID may not
    be present.
    """

    if source_id:
        return f"wise:{source_id}"

    raw = "\x1f".join(
        f"{key}={row.get(key, '')}"
        for key in sorted(row)
    )

    digest = hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()

    return f"wise:{digest}"


def raise_missing_date(
    source_id: str | None,
    source_row: int,
) -> date:
    raise ValueError(
        "Wise row has neither Created on nor Finished on: "
        f"source_id={source_id!r}, row={source_row}"
    )