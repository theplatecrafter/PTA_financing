# importer/paypay.py

import csv
import hashlib
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from .common import ParsedRecord


def parse(path: Path) -> list[ParsedRecord]:
    """
    Parse a PayPay transaction CSV into normalized ParsedRecord objects.

    This parser only reads and normalizes the CSV.
    It does not assign Beancount accounts or interpret transactions.
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

    timestamp = parse_timestamp(row["取引日"])

    transaction_id = clean(row.get("取引番号"))

    withdrawal = parse_money(row.get("出金金額（円）"))
    deposit = parse_money(row.get("入金金額（円）"))

    amount = determine_amount(
        withdrawal=withdrawal,
        deposit=deposit,
    )
    print(amount)

    foreign_amount = parse_money(row.get("海外出金金額"))
    foreign_currency = clean(row.get("通貨"))
    exchange_rate = parse_money(row.get("変換レート（円）"))

    description = clean(row.get("取引内容"))
    counterparty = clean(row.get("取引先"))

    transaction_date = timestamp.date()
    transaction_time = timestamp.time()

    record_id = make_record_id(
        transaction_id=transaction_id,
        row=row,
    )

    return ParsedRecord(
        record_id=record_id,
        source="paypay",
        source_file=source_file,
        source_row=source_row,
        source_id=transaction_id,

        date=transaction_date,
        time=transaction_time,
        completed_at=None,

        description=description,
        amount=amount,
        currency="JPY",

        balance=None,
        balance_currency="JPY",

        counterparty=counterparty,
        category=None,
        payment_method=clean(row.get("取引方法")),
        reference=None,
        note=None,
        tags=[],

        payment_type=clean(row.get("支払い区分")),
        installment_number=None,
        payment_amount=None,

        source_amount=foreign_amount,
        source_currency=foreign_currency,

        target_amount=(
            abs(amount)
            if foreign_amount is not None
            else None
        ),
        target_currency=(
            "JPY"
            if foreign_amount is not None
            else None
        ),

        exchange_rate=exchange_rate,
        conversion_date=None,

        fee_amount=None,
        fee_currency=None,

        raw_data=dict(row),
    )


def clean(value: str | None) -> str | None:
    """
    Strip whitespace and convert empty values to None.
    """
    if value is None:
        return None

    value = value.strip()

    if not value or value == "-":
        return None

    return value


def parse_timestamp(value: str) -> datetime:
    """
    Parse PayPay's timestamp.

    Example:
        2026/08/23 11:22:04
    """

    return datetime.strptime(
        value.strip(),
        "%Y/%m/%d %H:%M:%S",
    )


def parse_money(value: str | None) -> Decimal | None:
    """
    Parse PayPay money fields.

    Examples:
        "1,720" -> Decimal("1720")
        "-"     -> None
        ""      -> None
    """

    value = clean(value)

    if value is None:
        return None

    return Decimal(
        value.replace(",", "")
    )




def determine_amount(
    withdrawal: Decimal | None,
    deposit: Decimal | None,
) -> Decimal:

    if withdrawal is not None and deposit is not None:
        raise ValueError(
            "PayPay row contains both withdrawal and deposit: "
            f"withdrawal={withdrawal}, deposit={deposit}"
        )

    if withdrawal is not None:
        return -withdrawal

    if deposit is not None:
        return deposit

    raise ValueError(
        "PayPay row contains neither withdrawal nor deposit."
    )


def make_record_id(
    transaction_id: str | None,
    row: dict[str, str],
) -> str:
    """
    Generate a deterministic ID for one source observation.

    A PayPay transaction number can occur on multiple legitimate rows. For
    example, a purchase and its points reward share the same number. Include
    the row contents so those observations remain separate while an identical
    row imported from another CSV gets the same ID.
    """

    raw = "\x1f".join(
        f"{key}={row.get(key, '')}"
        for key in sorted(row)
    )

    digest = hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()

    if transaction_id:
        return f"paypay:{transaction_id}:{digest[:16]}"
    return f"paypay:{digest}"