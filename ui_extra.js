
function postingContainer(button) {
  return button.closest('.review-current, .detail-panel').querySelector('[id$="postings"]');
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
function quickCategorize(button) {
  const area = button.closest('.quick-entry');
  const source = area.querySelector('[data-quick="source"]').value;
  const target = area.querySelector('[data-quick="target"]').value;
  const direction = area.querySelector('[data-quick="direction"]').value;
  const amount = area.dataset.amount.trim().replace(/^[+-]/, '');
  if (!source || !target || source === target || !/^\d+(\.\d+)?$/.test(amount) || !area.dataset.currency) {
    alert('Choose two different accounts. A valid source amount and currency are required.');
    return;
  }
  fillPostings(button, [
    {account: source, amount: direction === '-1' ? '-' + amount : amount, currency: area.dataset.currency},
    {account: target, amount: direction === '-1' ? amount : '-' + amount, currency: area.dataset.currency}
  ]);
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
  if (event.target.form) showBalance(event.target.form);
});
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('form').forEach(showBalance);
});
