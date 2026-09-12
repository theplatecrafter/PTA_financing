"""Validation for the editor's explicit amount/currency postings."""
import re
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation

ACCOUNT = re.compile(r"^(Assets|Liabilities|Equity|Income|Expenses)(:[A-Z][A-Za-z0-9-]*)+$")
CURRENCY = re.compile(r"^[A-Z][A-Z0-9'._-]*$")


def validate(postings, accounts, transaction_date=None):
    if not postings or len(postings) < 2:
        raise ValueError('Add at least two postings before resolving.')
    if transaction_date:
        try:
            date.fromisoformat(transaction_date)
        except ValueError:
            raise ValueError("Use a valid transaction date.")
    totals = defaultdict(Decimal)
    for posting in postings:
        account, currency = posting.get('account', ''), posting.get('currency', '')
        if not ACCOUNT.fullmatch(account) or account not in accounts:
            raise ValueError(f'Create a valid account first: {account or "(empty account)"}')
        if not CURRENCY.fullmatch(currency):
            raise ValueError('Use a valid currency code, such as USD or JPY.')
        if accounts[account]['currency'] and currency not in accounts[account]['currency'].split(','):
            raise ValueError(f'{account} does not allow {currency}.')
        if transaction_date and transaction_date < accounts[account]['open_date']:
            raise ValueError(f'{account} opens after this transaction. Adjust its opening date in Accounts.')
        try:
            amount = Decimal(posting.get('amount', ''))
            if not amount.is_finite():
                raise InvalidOperation()
        except (InvalidOperation, TypeError):
            raise ValueError('Every posting needs a finite numeric amount.')
        totals[currency] += amount
    if any(total != 0 for total in totals.values()):
        raise ValueError('Postings must balance to zero in each currency. Save a draft while working on splits or currency conversions.')

