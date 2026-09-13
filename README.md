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

Open http://127.0.0.1:50001 for the importer. The Fava reports tab embeds Fava
at /fava on the same server and includes an open-in-new-tab button. No second
server is required. Set PORT=5001 to keep using the old port. Alternatively, run
`fava main.beancount` in a second terminal for reports at
http://127.0.0.1:5000. Keep the importer on localhost; it has no multi-user
authentication.

## Everyday workflow

1. **Accounts:** use **Import & export → Read from Beancount** to read existing
   declarations, or create accounts. Open them on or before the earliest
   transaction. Multiple currency restrictions use commas, e.g. USD,JPY.
2. **Import & export:** upload PayPay, SDFCU, Sumitomo bank, Sumitomo credit card,
   or Wise CSVs. Exact record duplicates are skipped.
3. **Review inbox:** use Quick categorize or accept a suggestion. Linked events
   also have Quick categorize: enter signed amounts for both accounts and select
   an optional fee account. The fee is the negative sum of the two postings.
   For example, -103 and +100 produce +3 in Expenses:Fees. When adding a third
   posting in the editor, its amount follows the first two until you manually
   edit the third amount. Different currencies are never netted into a fee.
   Use individual
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

- Add as many filters as needed, combined with ALL (AND) or ANY (OR).
  Source, currency, and amount limits always apply outside this group.
- Choose from every normalized ParsedRecord field, additional parsed fields
  discovered in imported records, and nested original CSV columns under raw_data.
  Existing single-filter rules continue to work and can be edited.
- Text comparisons include contains, equals, starts with, and their exclusions.
  Numeric comparisons include greater/less than and inclusive limits.
  Has a value and Is empty / missing support sparse fields. Zero is a value.
  Matching is literal and case insensitive; missing fields only match empty.
  Original nested column names containing slashes are preserved using JSON pointers.
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

The local model (v2) compares typed features independently: both description and
counterparty, payment method/type, category, notes, tags, amounts, balances
(low weight), installment details, source/target/fee currencies and amounts,
exchange rates, and original CSV columns. Source, primary currency and amount
sign constrain candidate examples. Models never pool unrelated bank sources.

Dates contribute weekday, month and day-of-month patterns; time contributes
time of day. Full dates, file locations and transaction IDs are not memorized.
Numbers use relative closeness. Text uses Unicode-normalized words and Japanese
character bigrams. Missing fields are not matches. Exact raw copies of normalized
values are suppressed, identifier columns are excluded, and the overall weight
of raw columns is capped so wide CSVs cannot dominate.

Weights start with more emphasis on descriptions, counterparties and payment
fields. Once a source has multiple confirmed account layouts, the model compares
within-layout and between-layout feature similarity to increase the weight of
discriminating fields and reduce unhelpful constant fields. Pair fitting is
deterministic and bounded to 120 stratified examples. A scan fits weights once
per source. This is a small explainable nearest-neighbor learner, not a neural
network or a calibrated classifier.

Examples must score at least 65% similarity, and voting uses the closest
neighborhood (within eight percentage points of the strongest match, preserving
ties). A suggestion requires three supporting examples and 80% weighted agreement.
That agreement is not a probability of correctness. Amount, balance, currency
and timing matches alone cannot establish an account prediction.

Predictions run when opening an eligible pending transaction or pressing
Run predictions now. Scans are read-only. Existing drafts, linked events, and
weak or conflicting evidence are left untouched. Suggestions display the fields
contributing evidence. The training panel shows usable examples and the
highest-weight fields per source.

Training snapshots store parsed features, the confirmed account layout, and
a signature of its postings. Only explicit human confirmations are labels.
Rule definitions and automatic resolutions are not labels; a rule suggestion
explicitly confirmed by a person can become one. Corrections replace examples.
Changed posting signatures are excluded until reconfirmed. Parsed-data edits
require Rebuild learned features. Disabling suggestions stops inference but
still records new human confirmations; Forget learned examples clears training.

Existing entries in the decisions table are automatically enriched from their
source records when the app starts. Rebuild learned features refreshes those
known human labels; it does not harvest arbitrary resolved/synced records.
Simple two-account matches can fill balanced amounts. Confirmed splits, fees
and conversions can train account-layout suggestions, but these leave amounts
blank for manual completion. Account and currency validation still applies.

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
