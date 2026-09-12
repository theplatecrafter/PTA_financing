# Clearledger — local finance workspace

Import statements, organize transactions, and keep a balanced plain-text
Beancount ledger. SQLite stores source observations and review decisions.
No financial data is sent to a hosted prediction service.

## Run in Debian / WSL

```sh
cd /home/hans/finances
python3 -m venv venv
source venv/bin/activate
pip install -r dependencies.txt
python -m importer.app
```

Open http://127.0.0.1:5001 for the importer. Run
`fava main.beancount` in a second terminal for reports at
http://127.0.0.1:5000. Keep the importer on localhost; it has no multi-user
authentication.

## Everyday workflow

1. **Accounts:** use **Import & export → Read from Beancount** to read existing
   declarations, or create accounts. Open them on or before the earliest
   transaction. Multiple currency restrictions use commas, e.g. USD,JPY.
2. **Import & export:** upload PayPay, SDFCU, Sumitomo bank, Sumitomo credit card,
   or Wise CSVs. Exact record duplicates are skipped.
3. **Review inbox:** use Quick categorize or accept a suggestion. Use individual
   postings for splits. Check the balance hint and resolve. Save drafts, hold
   uncertain items, or ignore irrelevant records.
4. **Transactions:** search and filter, revisit decisions, or select several
   records to append posting assignments as a batch.
5. **Export resolved transactions:** write accounts.beancount and
   posts.beancount. Startup never synchronizes or exports automatically.

A 25 USD purchase has Assets:Bank -25 USD and Expenses:Food 25 USD.
Income normally increases an asset and has a negative Income posting.
Transfers move between assets; card payments move from an asset to a liability
and must not become a second expense. Hold and link the two source observations
of a transfer so they export as one event.

The guided editor supports explicit postings balancing separately in each
currency. Priced foreign exchange, investment cost lots, and price/cost syntax
still need the plain-text editor or Fava. Complex imports should stay drafts
until fees and all balancing postings are accounted for.

## Rules & learning

Create a rule in its tab, or use **Create a rule from this transaction** after
categorizing a simple transaction.

- Match description, counterparty, or category with literal contains, equals,
  or starts-with text. Matching ignores case.
- Narrow by source, currency, reported amount sign, and absolute amount limits.
- Choose both posting accounts and the sign on the first account.
- Lower priority numbers run first; the first match wins. Preview shows
  competing rules, proposed postings, and validation problems.
- Suggestion rules need confirmation. Automatic rules resolve newly imported
  simple, untouched, unlinked records. Zero amounts, fees, and currency
  conversions remain for manual review.
- Explicitly apply selected automatic matches to existing pending records.
  Pause, edit, or delete rules. Review the latest 30 automatic decisions.
  Undo is available before a decision is edited, linked, or exported.
- Automatic decisions never write Beancount files; export is separate.

The local learning model uses token similarity and weighted voting among
confirmed account-pair decisions within the same source, currency, and amount
sign. It needs at least three similar examples and 80% weighted agreement.
Agreement is not a calibrated probability. Learned suggestions always require
confirmation; automatic decisions do not train the model. Corrections replace
the record's saved example. Turn suggestions off or forget examples in the
Rules tab. Turning suggestions off still records confirmed decisions, as
explained in the interface.

## Storage and export

Default state: state/importer.db. Set IMPORTER_DB_PATH for an isolated database.
Keep backups before using the advanced Database editor or direct file editor.

Generated transactions use importer markers. Repeated exports replace their
blocks without duplication, including linked groups and legacy record markers.
Returning an exported record to a draft removes its generated block on the
next export. Handwritten transaction content and account declarations absent
from SQLite are preserved. Matching declarations are updated from the account
editor, retaining trailing comments and metadata lines.

The direct file and database editors bypass parts of the guided workflow.
Guided postings and rules validate account names, opening dates, finite amounts,
currencies, and balances before resolving or exporting.

## Verification

```sh
venv/bin/python -m unittest discover -s tests -v
```

Tests use temporary databases and ledger files, including Beancount parsing of
generated entries. They do not change your financial records.

Accounting references:
- https://beancount.github.io/docs/
- https://beancount.github.io/docs/getting_started_with_beancount/
