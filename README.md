# CSV file importer currently supported for:
SDFCU banking

Sumitomo banking

Sumitomo credit card

Wise banking

PayPay



# Finance Import Manager
Install with:
```sh
python3 -m venv venv
source venv/bin/activate
pip install -r dependencies.txt
```

The local importer UI is started with:

```sh
source venv/bin/activate
fava main.beancount
```

The local fava-beancount UI is started with:

```sh
source venv/bin/activate
python -m importer.app
```


Open `http://127.0.0.1:5001`. The application stores importer state in
`state/importer.db` and does not synchronize Beancount files at startup.

Open `http://127.0.0.1:5000` for the fava Beancount UI. The `Import` tab shows the importer state and allows you to resolve source records into Beancount transactions.

`Execute to Beancount` writes resolved database records to `posts.beancount`
and the editable account tree to `accounts.beancount`. Generated transactions
are marked with importer metadata, so handwritten Beancount content is left
alone. `Sync/import with Beancount` performs the reverse operation for account
declarations and importer-generated transaction markers.

Accounts use colon-separated hierarchical names such as
`Assets:Japan:PayPay`. Linked source records remain separate observations in
SQLite but are exported as one grouped transaction after they are resolved.

The `Edit database` tab exposes the SQLite state tables directly. Choose a
table to edit rows, add rows, select multiple rows for deletion or duplication,
and confirm destructive deletion in the browser.
