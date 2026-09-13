let lastSearchCheckbox = null; function updateMergeDefaults(form) {
  const source = form.elements.source.selectedOptions[0];
  const destination = form.elements.destination.selectedOptions[0];
  const selected = destination?.value ? destination : source;
  if (!selected?.value) return;
  if (!form.elements.name.value || form.elements.name.dataset.auto === 'true') {
    form.elements.name.value = selected.value;
    form.elements.name.dataset.auto = 'true';
  }
  if (!form.elements.currency.value || form.elements.currency.dataset.auto === 'true') {
    form.elements.currency.value = selected.dataset.currency || '';
    form.elements.currency.dataset.auto = 'true';
  }
  if (!form.dataset.mergeDefaultsBound) {
    form.elements.name.addEventListener('input', () => form.elements.name.dataset.auto = 'false');
    form.elements.currency.addEventListener('input', () => form.elements.currency.dataset.auto = 'false');
    form.dataset.mergeDefaultsBound = 'true';
  }
  const dates = [source?.dataset.openDate, destination?.dataset.openDate].filter(Boolean).sort();
  form.elements.open_date.value = dates[0] || '';
}
function addPosting(target, button) {
  const container = document.getElementById(target) || button?.closest('form')?.querySelector('[id$="postings"]');
  if (!container) return;
  const source = container.querySelector('.posting');
  if (!source) return;
  const row = source.cloneNode(true);
  delete row.dataset.remainder;
  row.querySelectorAll('input').forEach(input => input.value = '');
  row.querySelectorAll('select').forEach(select => select.selectedIndex = 0);
  if (!container.querySelector('[name="posting_sign"]') && container.children.length === 2) {
    row.dataset.remainder = 'true';
    row.querySelector('[name="posting_amount"]').title = 'Fee remainder from the first two postings; edit to override';
  }
  container.append(row);
  updateFeeRemainder(container);
  showBalance(container.closest('form'));
}
 function addBulkPosting(button) { addPosting('bulk-postings', button) } function removePosting(button) { const container = button.closest('[id$="postings"]'); const rows = container ? container.querySelectorAll('.posting') : []; if (rows.length > 1) { button.closest('.posting').remove(); showBalance(container.closest('form')); } } function collectHeld() { const form = document.getElementById('held-link-form'); form.querySelectorAll('input[name="linked_record_id"]').forEach(input => input.remove()); document.querySelectorAll('input[name="held_record"]:checked').forEach(checked => { const input = document.createElement('input'); input.type = 'hidden'; input.name = 'linked_record_id'; input.value = checked.value; form.append(input) }); return form.querySelector('input[name="linked_record_id"]') !== null } function handleSearchCheckbox(event, checkbox) { event.stopPropagation(); const boxes = [...document.querySelectorAll('input[name="selected_record_id"]')]; if (event.shiftKey && lastSearchCheckbox) { const start = boxes.indexOf(lastSearchCheckbox); const end = boxes.indexOf(checkbox); if (start !== -1 && end !== -1) boxes.slice(Math.min(start, end), Math.max(start, end) + 1).forEach(box => box.checked = checkbox.checked) } lastSearchCheckbox = checkbox; updateSelection() } function updateSelection() { const boxes = document.querySelectorAll('input[name="selected_record_id"]:checked'); const count = document.getElementById('search-selection-count'); if (count) count.textContent = boxes.length + ' selected'; const bulk = document.getElementById('bulk-interpretation-panel'); const single = document.getElementById('single-interpretation-panel'); if (bulk && single) { const hasSelection = boxes.length > 0; bulk.hidden = !hasSelection; single.hidden = hasSelection } } function toggleAllSearch() { const boxes = document.querySelectorAll('input[name="selected_record_id"]'); const shouldSelect = [...boxes].some(box => !box.checked); boxes.forEach(box => box.checked = shouldSelect); lastSearchCheckbox = null; updateSelection() } function collectSearchSelection(form) { form.querySelectorAll('input[name="selected_record_id"]').forEach(input => input.remove()); document.querySelectorAll('input[name="selected_record_id"]:checked').forEach(checked => { const input = document.createElement('input'); input.type = 'hidden'; input.name = 'selected_record_id'; input.value = checked.value; form.append(input) }); return form.querySelector('input[name="selected_record_id"]') !== null }

function postingContainer(button) {
  return button.closest('.review-current, #single-interpretation-panel').querySelector('[id$="postings"]');
}
function fillPostings(button, postings) {
  const container = postingContainer(button);
  if (!container) return;
  if ([...container.querySelectorAll('[name="posting_amount"]')].some(input => input.value) &&
      !confirm('Replace the current posting draft?')) return;
  const prototype = container.querySelector('.posting').cloneNode(true);
  container.replaceChildren();
  postings.forEach(posting => {
    const row = prototype.cloneNode(true);
    delete row.dataset.remainder;
    row.querySelector('[name="posting_account"]').value = posting.account;
    row.querySelector('[name="posting_amount"]').value = posting.amount;
    row.querySelector('[name="posting_currency"]').value = posting.currency;
    container.append(row);
  });
  container.dispatchEvent(new Event('input', {bubbles: true}));
  container.querySelector('select').focus();
}
function useSuggestion(button) {
  fillPostings(button, JSON.parse(button.dataset.postings));
}
async function quickCategorize(button) {
  const area = button.closest('.quick-entry');
  const error = area.querySelector('.quick-error');
  error.hidden = true;
  const data = new FormData();
  area.querySelectorAll('[data-quick]').forEach(input => data.set(input.dataset.quick, input.value));
  if (!data.get('target_amount').trim()) {
    const value = data.get('source_amount').trim();
    data.set('target_amount', value.startsWith('-') ? value.slice(1) : '-' + value.replace(/^\+/, ''));
  }
  button.disabled = true;
  try {
    const response = await fetch(area.dataset.previewUrl, {method: 'POST', body: data});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Could not build postings.');
    fillPostings(button, result.postings);
  } catch (problem) {
    error.textContent = problem.message || 'Could not reach the app. Try again.';
    error.hidden = false;
  } finally {
    button.disabled = false;
  }
}

function updateCondition(select) {
  const input = select.closest('.rule-condition').querySelector('[name="condition_pattern"]');
  const needsValue = !['empty', 'exists'].includes(select.value);
  input.required = false;
  input.readOnly = !needsValue;
  input.placeholder = needsValue ? 'Matching value' : 'No value needed';
}
function addRuleCondition() {
  const container = document.getElementById('rule-conditions');
  const row = container.querySelector('.rule-condition').cloneNode(true);
  row.querySelector('[name="condition_pattern"]').value = '';
  container.append(row);
  updateCondition(row.querySelector('[name="condition_operator"]'));
  row.querySelector('select').focus();
}
function removeRuleCondition(button) {
  const row = button.closest('.rule-condition');
  if (row.parentElement.children.length > 1) row.remove();
}

// Compute the third posting from the first two using exact decimal integers.
function updateFeeRemainder(container) {
  const rows = [...container.querySelectorAll('.posting')];
  if (rows.length !== 3 || !rows[2].dataset.remainder) return;
  const first = rows.slice(0, 2).map(row => ({
    amount: row.querySelector('[name="posting_amount"]').value.trim(),
    currency: row.querySelector('[name="posting_currency"]').value.trim()
  }));
  if (!first[0].currency || first[0].currency !== first[1].currency ||
      first.some(p => !/^[+-]?\d+(\.\d+)?$/.test(p.amount))) {
    rows[2].querySelector('[name="posting_amount"]').value = '';
    return;
  }
  const scale = Math.max(...first.map(p => (p.amount.split('.')[1] || '').length));
  const total = first.reduce((sum, p) => {
    const [whole, fraction = ''] = p.amount.replace(/^[+-]/, '').split('.');
    return sum + BigInt(whole + fraction.padEnd(scale, '0')) * (p.amount.startsWith('-') ? -1n : 1n);
  }, 0n);
  const remainder = -total;
  let digits = (remainder < 0n ? -remainder : remainder).toString().padStart(scale + 1, '0');
  if (scale) digits = digits.slice(0, -scale) + '.' + digits.slice(-scale);
  rows[2].querySelector('[name="posting_amount"]').value = (remainder < 0n ? '-' : '') + digits;
  rows[2].querySelector('[name="posting_currency"]').value = first[0].currency;
}
// Exact decimal arithmetic avoids floating point rounding in the balance hint.
function showBalance(form) {
  const rows = [...form.querySelectorAll('.posting')];
  if (!rows.length || form.querySelector('[name="posting_sign"]')) return;
  let output = form.querySelector('.balance-feedback');
  if (!output) {
    output = document.createElement('p');
    output.className = 'balance-feedback muted';
    output.setAttribute('aria-live', 'polite');
    rows[0].parentElement.after(output);
  }
  const totals = new Map();
  let complete = true;
  rows.forEach(row => {
    const amount = row.querySelector('[name="posting_amount"]').value.trim();
    const currency = row.querySelector('[name="posting_currency"]').value.trim();
    const account = row.querySelector('[name="posting_account"]').value;
    if (!/^[+-]?\d+(\.\d+)?$/.test(amount) || !currency || !account) { complete = false; return; }
    const pieces = amount.replace(/^[+-]/, '').split('.');
    const scale = (pieces[1] || '').length;
    const integer = BigInt(pieces[0] + (pieces[1] || '')) * (amount.startsWith('-') ? -1n : 1n);
    const old = totals.get(currency) || {value: 0n, scale: 0};
    const nextScale = Math.max(scale, old.scale);
    totals.set(currency, {value: old.value * 10n ** BigInt(nextScale-old.scale) + integer * 10n ** BigInt(nextScale-scale), scale: nextScale});
  });
  const balanced = complete && rows.length >= 2 && [...totals.values()].every(t => t.value === 0n);
  output.textContent = balanced ? '✓ Balanced — ready to resolve.' : 'Complete at least two postings that total zero in each currency. You can save a draft anytime.';
  output.style.color = balanced ? 'var(--accent)' : 'var(--muted)';
}
document.addEventListener('input', event => {
  const posting = event.target.closest('.posting');
  if (posting) {
    if (event.target.name === 'posting_amount') delete posting.dataset.remainder;
    updateFeeRemainder(posting.parentElement);
  }
  const form = event.target.form || event.target.closest('form');
  if (form) showBalance(form);
});
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('form').forEach(showBalance);
  document.querySelectorAll('[name="condition_operator"]').forEach(updateCondition);
});

function updateRuleAction(select) {
  const form = select.form;
  const single = select.value === 'account';
  form.querySelector('[data-account-action]').hidden = !single;
  form.querySelector('[data-transaction-action]').hidden = single;
  form.querySelectorAll('[data-transaction-action] [required]').forEach(el => el.required = false);
  form.querySelectorAll('[data-transaction-action] select').forEach(el => el.disabled = single);
  form.querySelectorAll('[data-account-action] select').forEach(el => el.disabled = !single);
}
async function previewFilters(button) {
  const dialog = document.getElementById('filter-preview');
  const output = dialog.querySelector('.filter-results');
  output.textContent = 'Searching…';
  dialog.showModal();
  try {
    const response = await fetch(button.dataset.url, {method: 'POST', body: new FormData(button.form)});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Search failed.');
    output.replaceChildren();
    const count = document.createElement('p');
    count.textContent = data.total + ' matching transactions';
    output.append(count);
    for (const record of data.records) {
      const link = document.createElement('a');
      link.className = 'filter-result';
      link.href = '/?' + new URLSearchParams({tab:'search',record:record.record_id});
      link.target = '_blank'; link.rel = 'noopener';
      link.textContent = [record.transaction_date,record.description,record.source,record.amount,record.currency,record.status].join(' · ');
      output.append(link);
    }
  } catch (error) { output.textContent = error.message; }
}
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('select[name="action"]').forEach(updateRuleAction);
});
