import csv
import hashlib
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from .common import ParsedRecord


HEADERS = [
    "ご利用日",
    "ご利用店名",
    "ご利用金額",
    "支払い区分",
    "今回階数",
    "お支払金額",
    "foreign_currency",
]


def parse(path: Path) -> list[ParsedRecord]:
    records: list[ParsedRecord] = []

    with path.open("r", encoding="cp932", newline="") as file:
        reader = csv.reader(file)

        for row_number, row in enumerate(reader, start=1):
            if not row:
                continue

            if not is_transaction_row(row):
                continue

            records.append(
                parse_row(
                    row=row,
                    source_file=path,
                    source_row=row_number,
                )
            )

    return records


def is_transaction_row(row: list[str]) -> bool:
    """
    Identify actual transaction rows.

    Metadata rows at the beginning of the CSV do not start
    with a date.
    """

    if not row:
        return False

    return bool(
        re.fullmatch(
            r"\d{4}/\d{1,2}/\d{1,2}",
            row[0].strip(),
        )
    )


def parse_row(
    row: list[str],
    source_file: Path,
    source_row: int,
) -> ParsedRecord:

    transaction_date = parse_date(row[0])
    description = clean(row[1])
    amount = parse_money(row[2])

    payment_type = clean(row[3])
    installment_number = parse_integer(row[4])
    payment_amount = parse_money(row[5])

    foreign_currency = (
        clean(row[6])
        if len(row) > 6
        else None
    )

    (
        source_amount,
        source_currency,
        exchange_rate,
        conversion_date,
    ) = parse_foreign_currency(
        foreign_currency,
        transaction_date,
    )

    record_id = make_record_id(row)

    raw_data = {
        f"column_{index + 1}": value
        for index, value in enumerate(row)
    }

    return ParsedRecord(
        record_id=record_id,
        source="sumitomo_credit_card",
        source_file=source_file,
        source_row=source_row,
        source_id=None,

        date=transaction_date,
        time=None,
        completed_at=None,

        description=description,
        amount=amount,
        currency="JPY",

        balance=None,
        balance_currency="JPY",

        payment_type=payment_type,
        installment_number=installment_number,
        payment_amount=payment_amount,

        counterparty=None,
        category=None,
        payment_method=None,
        reference=None,
        note=None,
        tags=[],

        source_amount=source_amount,
        source_currency=source_currency,
        target_amount=amount if source_amount is not None else None,
        target_currency="JPY" if source_amount is not None else None,
        exchange_rate=exchange_rate,
        conversion_date=conversion_date,

        fee_amount=None,
        fee_currency=None,

        raw_data=raw_data,
    )


def clean(value: str | None) -> str | None:
    if value is None:
        return None

    value = value.strip()

    return value if value else None


def parse_date(value: str) -> date:
    return datetime.strptime(
        value.strip(),
        "%Y/%m/%d",
    ).date()


def parse_money(value: str | None) -> Decimal | None:
    value = clean(value)

    if value is None:
        return None

    return Decimal(
        value.replace(",", "")
    )


def parse_integer(value: str | None) -> int | None:
    value = clean(value)

    if value is None:
        return None

    return int(value)


def parse_foreign_currency(
    value: str | None,
    transaction_date: date,
) -> tuple[
    Decimal | None,
    str | None,
    Decimal | None,
    date | None,
]:
    """
    Parse the optional foreign-currency field.

    Example:

        2739.00　JPY　1.0000　05 28

    becomes:

        source_amount = 2739.00
        source_currency = JPY
        exchange_rate = 1.0000
        conversion_date = May 28
    """

    value = clean(value)

    if value is None:
        return None, None, None, None

    # Sumitomo uses full-width Japanese spaces.
    parts = re.split(r"\s+", value)

    if len(parts) != 5:
        raise ValueError(
            f"Unexpected foreign-currency format: {value!r}"
        )

    source_amount = Decimal(parts[0])
    source_currency = parts[1]
    exchange_rate = Decimal(parts[2])

    month, day = map(int, parts[3:5])

    conversion_date = date(
        transaction_date.year,
        month,
        day,
    )

    return (
        source_amount,
        source_currency,
        exchange_rate,
        conversion_date,
    )


def make_record_id(row: list[str]) -> str:
    """
    Generate a deterministic ID from the complete source row.
    """

    raw = "\x1f".join(row)

    digest = hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()

    return f"sumitomo_credit_card:{digest}"