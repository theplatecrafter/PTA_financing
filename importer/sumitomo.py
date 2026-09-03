# importer/sumitomo.py

import csv
import hashlib
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from .common import ParsedRecord


def parse(path: Path) -> list[ParsedRecord]:
    """
    Parse a Sumitomo Bank CSV into normalized ParsedRecords.
    """

    records: list[ParsedRecord] = []

    with path.open("r", encoding="cp932", newline="") as file:
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

    transaction_date = parse_date(row["年月日"])

    withdrawal = parse_money(row.get("お引出し"))
    deposit = parse_money(row.get("お預入れ"))
    balance = parse_money(row.get("残高"))

    amount = determine_amount(
        withdrawal=withdrawal,
        deposit=deposit,
    )

    description = clean(row.get("お取り扱い内容"))
    memo = clean(row.get("メモ"))
    label = clean(row.get("ラベル"))

    record_id = make_record_id(
        row=row,
    )

    return ParsedRecord(
        record_id=record_id,
        source="sumitomo_bank",
        source_file=source_file,
        source_row=source_row,
        source_id=None,

        date=transaction_date,
        time=None,
        completed_at=None,

        description=description,
        amount=amount,
        currency="JPY",

        balance=balance,
        balance_currency="JPY",

        counterparty=None,
        category=None,
        payment_method=None,
        reference=None,
        note=memo,
        tags=[],

        payment_type=None,
        installment_number=None,
        payment_amount=None,

        source_amount=None,
        source_currency=None,
        target_amount=None,
        target_currency=None,

        exchange_rate=None,
        conversion_date=None,

        fee_amount=None,
        fee_currency=None,

        raw_data=dict(row),
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


def determine_amount(
    withdrawal: Decimal | None,
    deposit: Decimal | None,
) -> Decimal:

    if withdrawal is not None and deposit is not None:
        raise ValueError(
            "Sumitomo row contains both withdrawal and deposit: "
            f"withdrawal={withdrawal}, deposit={deposit}"
        )

    if withdrawal is not None:
        return -withdrawal

    if deposit is not None:
        return deposit

    raise ValueError(
        "Sumitomo row contains neither withdrawal nor deposit."
    )


def make_record_id(
    row: dict[str, str],
) -> str:

    raw = "\x1f".join(
        f"{key}={row.get(key, '')}"
        for key in sorted(row)
    )

    digest = hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()

    return f"sumitomo_bank:{digest}"