from pathlib import Path
import re
for name in ('index', 'files', 'database'):
    p = Path('importer/templates') / (name + '.html')
    s = p.read_text()
    content = s.split('<main>', 1)[1].split('</main>', 1)[0]
    content = content.split('{% endwith %}', 1)[1]
    scripts = re.findall(r'<script>(.*?)</script>', s, re.S)
    if name == 'index':
        Path('importer/static/app.js').write_text('\n'.join(scripts) + '\n')
    elif scripts:
        content += ''.join('<script>' + script + '</script>' for script in scripts)
    p.write_text("{% extends 'base.html' %}\n{% block content %}\n" + content.strip() + "\n{% endblock %}\n")
p = Path('importer/templates/index.html')
s = p.read_text()
start = s.index('<section class="panel">', s.index("{% elif tab == 'stats' %}"))
end = s.index("{% elif tab == 'review' %}", start)
s = s[:start] + "{% include 'overview.html' %}\n" + s[end:]
s = s.replace('Save and keep pending</button><button name="status"\n', 'Save draft</button>\n')
s = s.replace('<p class="eyebrow">ACCOUNTING</p>', """<p class="eyebrow">CATEGORIZE THIS TRANSACTION</p>
{% include "quick_entry.html" %}""")
s = s.replace('<h3>Postings</h3>', """{% include "quick_entry.html" %}
<h3>Postings</h3>""")
s = s.replace('Save as resolved', 'Resolve transaction').replace('Save as\n                                resolved', 'Resolve transaction')
s = s.replace('Create any colon-separated hierarchy. Parent directories can be added later.', 'Use Assets for money you own, Liabilities for debts, Expenses for spending, and Income for earnings. Example: Expenses:Food:Groceries. Open accounts before your earliest transaction.')
s = s.replace('Search records', 'Transactions')
s = s.replace('placeholder="Search all parsed record fields"', 'aria-label="Search transactions" placeholder="Search merchant, description, or source…"')
s = s.replace('<select name="status">', '<select name="status" aria-label="Transaction status">')
s = s.replace('<select name="posting_account">', '<select name="posting_account" aria-label="Posting account">')
s = s.replace('<input name="posting_amount"', '<input name="posting_amount" aria-label="Posting amount" inputmode="decimal"')
s = s.replace('<input name="posting_currency"', '<input name="posting_currency" aria-label="Posting currency"')
s = s.replace('name="selected_record_id" value=', 'aria-label="Select transaction" name="selected_record_id" value=')
s = s.replace("""{% if tab != 'search' %}<div class="empty-state">""", '<div class="empty-state">')
s = s.replace('Choose a record from the left to interpret or link it.</p></div>{% endif %}', 'Choose a transaction to view its details and assign accounts, or select several to categorize them together.</p></div>')
s = s.replace('Execute to Beancount', 'Export resolved transactions').replace('Sync/import with Beancount', 'Read from Beancount')
s = s.replace('Execute to\n                                    Beancount', 'Export resolved transactions').replace('Sync/import with\n                                    Beancount', 'Read from Beancount')
s = s.replace("""<h1>{{ selected['source'] }}</h1>""", """<h1>{{ selected['description'] or selected['source'] }}</h1>""")
s = s.replace('<h2>No pending records</h2>', """<h2>You’re all caught up.</h2><p>Import a new statement, or revisit transactions on hold.</p><a class="button primary" href="{{ url_for('index',tab='sync') }}">Import a statement</a>""")
s = s.replace('<div class="manual-grid">{% for field, label, field_type in manual_fields %}', '<div class="manual-grid">{% for field, label, field_type in manual_fields[:9] %}')
s = s.replace('%}</label>{% endfor %}</div><button class="primary">Create draft</button>', """%}</label>{% endfor %}</div>
<details><summary>Additional details · fees, exchange rates, references</summary><div class="manual-grid">
{% for field,label,field_type in manual_fields[9:] %}<label>{{ label }}<input type="{{ field_type }}" name="{{ field }}"></label>{% endfor %}
</div></details><button class="primary spaced">Create draft</button>""")
p.write_text(s)
