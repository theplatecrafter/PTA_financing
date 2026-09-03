# Updated: 2026/8/27

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any


@dataclass
class ParsedRecord:
    """
    A normalized observation of one record from an external
    financial institution.

    This class describes WHAT THE SOURCE REPORTED.

    It does not describe:
        - which Beancount account it belongs to
        - whether it is income or an expense
        - whether it is a transfer
        - whether it is a duplicate
        - what the other side of the transaction is
    """

    # ------------------------------------------------------------------
    # Identity / provenance
    # ------------------------------------------------------------------

    record_id: str
    source: str
    source_file: Path
    source_row: int
    source_id: str | None

    # ------------------------------------------------------------------
    # Time
    # ------------------------------------------------------------------

    date: date
    time: time | None
    completed_at: datetime | None

    # ------------------------------------------------------------------
    # Primary transaction information
    # ------------------------------------------------------------------

    description: str | None
    amount: Decimal | None
    currency: str | None

    # Balance after this record, when the source provides one
    balance: Decimal | None
    balance_currency: str | None

    # ------------------------------------------------------------------
    # Descriptive information
    # ------------------------------------------------------------------

    counterparty: str | None
    category: str | None
    payment_method: str | None
    reference: str | None
    note: str | None
    tags: list[str]

    # ------------------------------------------------------------------
    # Payment / installment information
    # ------------------------------------------------------------------

    payment_type: str | None
    installment_number: int | None
    payment_amount: Decimal | None

    # ------------------------------------------------------------------
    # Foreign currency / exchange information
    # ------------------------------------------------------------------

    source_amount: Decimal | None
    source_currency: str | None

    target_amount: Decimal | None
    target_currency: str | None

    exchange_rate: Decimal | None
    conversion_date: date | None

    # ------------------------------------------------------------------
    # Fees
    # ------------------------------------------------------------------

    fee_amount: Decimal | None
    fee_currency: str | None

    # ------------------------------------------------------------------
    # Complete original source row
    # ------------------------------------------------------------------

    raw_data: dict[str, Any]