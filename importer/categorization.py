"""Exact signed-amount helpers for two accounts and an optional fee remainder."""
from decimal import Decimal, InvalidOperation
from . import database


def quick_postings(path, values, transaction_date):
    def amount(key):
        try:
            value = Decimal(values.get(key, "").strip())
            if not value.is_finite():
                raise InvalidOperation()
            return value
        except (InvalidOperation, AttributeError):
            raise ValueError("Enter a finite signed amount for each of the two accounts.")
    source, target = values.get("source", ""), values.get("target", "")
    fee = values.get("fee", "")
    if not source or not target or source == target or fee in (source, target):
        raise ValueError("Choose different accounts for each side and the optional fee.")
    left, right = amount("source_amount"), amount("target_amount")
    currency = values.get("source_currency", "").strip()
    other_currency = values.get("target_currency", "").strip()
    if currency != other_currency:
        raise ValueError("Different currencies cannot be netted into a fee. Use the posting editor for currency conversion.")
    postings = [dict(account=source, amount=str(left), currency=currency),
                dict(account=target, amount=str(right), currency=currency)]
    remainder = -(left + right)
    if fee:
        postings.append(dict(account=fee, amount=str(remainder), currency=currency))
    elif remainder:
        raise ValueError(f"The two accounts differ by {remainder} {currency}. Choose a fee account or correct the amounts.")
    database.validate_postings(path, postings, transaction_date)
    return postings
