'use strict';
const el = (id) => document.getElementById(id);
const hosted = document.documentElement.classList.contains('wz-hosted');
let ov = null, rows = [], tab = 'review', cache = {}, month = new Date().toISOString().slice(0, 7), cur = null, pick = null, poll = null, timer = null;

function say(text, kind = '', undo = false) {
  const s = el('status'); s.classList.toggle('wz-sr', !kind); s.classList.toggle('note', kind === 'note'); s.replaceChildren(text);
  if (undo) { const b = h('button', 'mn-undo', 'Undo'); b.type = 'button'; b.onclick = doUndo; s.append(' ', b); }
}
async function api(path, body) {
  const init = body === undefined ? { cache: 'no-store' } : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
  const r = await fetch(path, init);
  let out = {};
  try { out = await r.json(); } catch (e) {}
  if (!r.ok) throw new Error(out.error || 'Something went wrong. Try again.');
  return out;
}
/* Show the last answer instantly, then refresh it quietly (stale-while-revalidate). */
const CK = 'money.cache.v1';
try { cache = JSON.parse(localStorage.getItem(CK) || '{}'); } catch (e) { cache = {}; }
let saveT = null;
function remember(path, data) {
  cache[path] = data;
  clearTimeout(saveT);
  saveT = setTimeout(() => {
    const keep = {}; for (const k of Object.keys(cache).slice(-12)) keep[k] = cache[k];
    cache = keep;
    try { localStorage.setItem(CK, JSON.stringify(keep)); } catch (e) {}
  }, 300);
  return data;
}
async function get(path, paint) {
  if (cache[path]) paint(cache[path], true);
  const fresh = remember(path, await api(path));
  paint(fresh, false);
  return fresh;
}
function h(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
const money = (n, cur) => n == null ? '' : new Intl.NumberFormat(undefined, { style: 'currency', currency: cur || 'USD' }).format(n);
const day = (d) => d ? new Date(d + 'T12:00:00').toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) : '';
const when = (iso) => iso ? new Date(iso).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }) : 'not yet';

/* ---------- overview ---------- */
async function loadOverview() {
  await get('api/overview', paintOverview);
}
function paintOverview(data) {
  ov = data;
  const ready = ov.transactions > 0 || ov.accounts.length > 0;
  const anyConnected = ov.sources.some((s) => s.connected);
  el('setup').hidden = ready || anyConnected || ov.syncing;
  el('badge').hidden = !ov.to_review; el('badge').textContent = ov.to_review > 99 ? '99+' : ov.to_review;
  try { window.WizardApp?.badge?.set(ov.to_review); } catch (e) {}
  renderAccounts();
  clearTimeout(poll);
  if (ov.syncing) poll = setTimeout(() => refresh(), 4000);
  if (tab === 'review') renderFilterChips();
}

/* ---------- review: filter and sort ---------- */
const FKEY = 'money.filter.v1';
const DEF = { show: 'todo', sort: 'newest', direction: '', when: '', category: '', account: '', origin: '' };
let f = { ...DEF };
try { f = { ...DEF, ...JSON.parse(localStorage.getItem(FKEY) || '{}') }; } catch (e) {}
const saveF = () => { try { localStorage.setItem(FKEY, JSON.stringify(f)); } catch (e) {} };
const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
function range(w) {
  const t = new Date();
  if (w === 'month') return [iso(new Date(t.getFullYear(), t.getMonth(), 1)), iso(t)];
  if (w === 'last') return [iso(new Date(t.getFullYear(), t.getMonth() - 1, 1)), iso(new Date(t.getFullYear(), t.getMonth(), 0))];
  if (w === '30' || w === '90') return [iso(new Date(t - (+w) * 864e5)), iso(t)];
  if (/^\d{4}-\d{2}$/.test(w)) { const [y, m] = w.split('-').map(Number); return [w + '-01', iso(new Date(y, m, 0))]; }
  return [null, null];
}
const monthName = (w) => new Date(w + '-15').toLocaleDateString(undefined, { month: 'long', year: 'numeric' });
function groups() {
  const cats = ['Uncategorized', ...(ov?.categories || [])];
  const accts = (ov?.accounts || []).map((a) => [a.id, a.name]);
  const g = [
    ['show', 'Show', [['todo', 'To review'], ['all', 'Everything']]],
    ['sort', 'Sort', [['newest', 'Newest first'], ['spend', 'Costs the most'], ['income', 'Biggest money in'], ['oldest', 'Oldest first']]],
    ['direction', 'Money', [['', 'In and out'], ['out', 'Money out'], ['in', 'Money in']]],
    ['when', 'When', [['', 'Any time'], ['month', 'This month'], ['last', 'Last month'], ['30', 'Last 30 days'], ['90', 'Last 90 days']]],
    ['category', 'Category', [['', 'Any category'], ...cats.map((c) => [c, c])], true],
    ['account', 'Account', [['', 'Any account'], ...accts], true],
  ];
  if (ov?.history) g.push(['origin', 'Source', [['', 'Both'], ['bank', 'From ' + ov.source_name], ['import', ov.history.from_name + ' history']]]);
  return g;
}
function label(key, val) {
  if (key === 'when' && /^\d{4}-\d{2}$/.test(val)) return monthName(val);
  const g = groups().find((x) => x[0] === key);
  const o = g && g[2].find((x) => x[0] === val);
  return o ? o[1] : val;
}
function renderFilterChips() {
  const box = el('fchips'); box.replaceChildren();
  let n = 0;
  for (const k of ['show', 'sort', 'direction', 'when', 'category', 'account', 'origin']) {
    if (f[k] === DEF[k] || (k === 'show')) continue;
    n++;
    const b = h('button', 'mn-chip on', label(k, f[k])); b.type = 'button';
    b.setAttribute('aria-label', 'Remove ' + label(k, f[k]));
    b.onclick = () => { f[k] = DEF[k]; saveF(); renderFilterChips(); loadList().catch((e) => say(e.message, 'error')); };
    box.append(b);
  }
  if (f.show === 'all') n++;
  el('fcount').hidden = !n; el('fcount').textContent = n;
  el('fbtn').querySelector('span').textContent = f.show === 'all' ? 'Everything' : 'To review';
}
function query() {
  const q = new URLSearchParams({ limit: 500 });
  if (f.show === 'todo') q.set('reviewed', 'false');
  if (f.sort !== 'newest') q.set('sort', f.sort);
  for (const k of ['direction', 'category', 'account', 'origin']) if (f[k]) q.set(k, f[k]);
  const [from, to] = range(f.when);
  if (from) { q.set('from', from); q.set('to', to); }
  const s = el('search').value.trim(); if (s) q.set('search', s);
  return 'api/transactions?' + q;
}
let listSeq = 0;
async function loadList() {
  const seq = ++listSeq;
  await get(query(), (r, stale) => { if (seq === listSeq) paintList(r, stale); });
}
function row(t, showDate) {
  const li = h('li', 'mn-row' + (t.reviewed ? ' is-reviewed' : ''));
  const btn = h('button', 'mn-row-b'); btn.type = 'button'; btn.onclick = () => openSheet(t);
  if (showDate) btn.append(h('span', 'mn-row-d', day(t.date)));
  const left = h('span', 'mn-row-n');
  const sub = h('small', '');
  if (t.origin === 'import') sub.append(h('span', 'mn-tag import', 'History'), ' ');
  sub.append([t.category || 'Uncategorized', t.account_name].filter(Boolean).join(' · ') + (t.status === 'pending' ? ' · pending' : ''));
  left.append(h('b', '', t.merchant || t.description), sub);
  btn.append(left, h('span', 'mn-amt' + (t.amount > 0 ? ' pos' : ''), money(t.amount)));
  const tick = h('button', 'mn-tick' + (t.reviewed ? ' on' : ''), '✓'); tick.type = 'button';
  tick.setAttribute('aria-label', t.reviewed ? 'Mark not reviewed' : 'Mark reviewed');
  tick.onclick = async () => {
    t.reviewed = !t.reviewed; li.classList.toggle('is-reviewed', t.reviewed); tick.classList.toggle('on', t.reviewed);
    try { await api('api/update', { source: t.source, id: t.id, reviewed: t.reviewed }); say(t.reviewed ? 'Reviewed.' : 'Marked to review.', 'note', true); refresh(); } catch (e) { say(e.message, 'error'); }
  };
  li.append(btn, tick);
  return li;
}
function paintList(r, stale) {
  rows = r.transactions;
  const box = el('list'); box.replaceChildren();
  const byDay = f.sort === 'newest' || f.sort === 'oldest';
  if (byDay) {
    let lastDay = null, group = null;
    for (const t of rows) {
      if (t.date !== lastDay) { lastDay = t.date; box.append(h('div', 'mn-day', day(t.date))); group = h('ul', 'mn-rows'); box.append(group); }
      group.append(row(t, false));
    }
  } else if (rows.length) {
    box.append(h('div', 'mn-day', `${label('sort', f.sort)} · ${r.total} item${r.total === 1 ? '' : 's'}`));
    const ul = h('ul', 'mn-rows'); for (const t of rows) ul.append(row(t, true)); box.append(ul);
  }
  const todo = f.show === 'todo';
  el('all-done').hidden = !(todo && rows.length > 1);
  el('all-done').textContent = `Mark these ${rows.length} reviewed`;
  el('empty').hidden = rows.length > 0 || stale;
  const filtered = ['sort', 'direction', 'when', 'category', 'account', 'origin'].some((k) => f[k] !== DEF[k]) || el('search').value.trim();
  el('empty').textContent = ov && ov.syncing ? 'Pulling your transactions…' : todo && !filtered ? 'All caught up. Nothing left to review.' : 'No transactions match.';
}

/* filter sheet: changes apply live, so you see the list change behind it */
let fTimer = null;
function openFilters() {
  const box = el('fgroups'); box.replaceChildren();
  for (const [key, title, opts, asSelect] of groups()) {
    const g = h('div', 'mn-fgroup'); g.append(h('label', 'mn-lab', title));
    if (asSelect) {
      const sel = h('select', 'wz-input'); sel.setAttribute('aria-label', title);
      for (const [v, t] of opts) { const o = h('option', '', t); o.value = v; sel.append(o); }
      sel.value = f[key]; if (sel.value !== f[key]) sel.value = '';
      sel.onchange = () => setF(key, sel.value);
      g.append(sel);
    } else {
      const chips = h('div', 'mn-chips');
      const list = key === 'when' && /^\d{4}-\d{2}$/.test(f.when) ? [...opts, [f.when, monthName(f.when)]] : opts;
      for (const [v, t] of list) {
        const b = h('button', 'mn-chip' + (f[key] === v ? ' on' : ''), t); b.type = 'button';
        b.onclick = () => { chips.querySelectorAll('.mn-chip').forEach((x) => x.classList.toggle('on', x === b)); setF(key, v); };
        chips.append(b);
      }
      g.append(chips);
    }
    box.append(g);
  }
  el('f-n').textContent = '';
  el('fsheet').hidden = false;
  countF();
}
function setF(key, v) { f[key] = v; saveF(); renderFilterChips(); clearTimeout(fTimer); fTimer = setTimeout(() => { loadList().then(countF).catch((e) => say(e.message, 'error')); }, 120); }
function countF() { const r = cache[query()]; el('f-n').textContent = r ? `${r.total} transaction${r.total === 1 ? '' : 's'}` : ''; }
const closeFilters = () => { el('fsheet').hidden = true; };
el('fbtn').onclick = openFilters;
el('f-done').onclick = closeFilters;
el('f-reset').onclick = () => { f = { ...DEF }; saveF(); renderFilterChips(); openFilters(); loadList().then(countF).catch((e) => say(e.message, 'error')); };
el('fsheet').onclick = (e) => { if (e.target === el('fsheet')) closeFilters(); };
renderFilterChips();

function openSheet(t) {
  cur = t; pick = t.category;
  el('t-merchant').textContent = t.merchant || t.description;
  el('t-meta').textContent = [day(t.date), t.account_name, t.status === 'pending' ? 'pending' : null, 'from ' + t.origin_name].filter(Boolean).join(' · ') + (t.description && t.description !== t.merchant ? ' — ' + t.description : '');
  el('t-amount').textContent = money(t.amount); el('t-amount').classList.toggle('pos', t.amount > 0);
  el('t-name').value = t.merchant || ''; el('t-note').value = t.note || ''; el('t-rule').checked = false;
  el('t-rule-text').textContent = `Always use this for “${(t.merchant || t.description || '').slice(0, 30)}”`;
  const chips = el('t-cats'); chips.replaceChildren();
  for (const c of ov.categories) {
    const b = h('button', 'mn-chip' + (c === pick ? ' on' : ''), c); b.type = 'button';
    b.onclick = () => { pick = c; chips.querySelectorAll('.mn-chip').forEach((x) => x.classList.toggle('on', x === b)); };
    chips.append(b);
  }
  el('t-hide').textContent = t.hidden ? 'Unhide' : 'Hide';
  el('sheet').hidden = false;
}
const closeSheet = () => { el('sheet').hidden = true; cur = null; };
el('t-close').onclick = closeSheet;
el('sheet').onclick = (e) => { if (e.target === el('sheet')) closeSheet(); };
el('t-save').onclick = async () => {
  const t = cur; if (!t) return;
  const body = { source: t.source, id: t.id, reviewed: true, note: el('t-note').value };
  const name = el('t-name').value.trim();
  if (name && name !== t.merchant) body.merchant = name;
  if (pick && pick !== t.category) body.category = pick;
  if (el('t-rule').checked && pick) { body.category = pick; body.make_rule = true; body.match = (t.merchant || t.description || '').slice(0, 40); }
  try { await api('api/update', body); closeSheet(); say(body.make_rule ? 'Saved, and future ones will match.' : 'Saved.', 'note', true); refresh(); } catch (e) { say(e.message, 'error'); }
};
el('t-hide').onclick = async () => {
  const t = cur; if (!t) return;
  try { await api('api/update', { source: t.source, id: t.id, hidden: !t.hidden, reviewed: true }); closeSheet(); say(t.hidden ? 'Unhidden.' : 'Hidden from lists and totals.', 'note', true); refresh(); } catch (e) { say(e.message, 'error'); }
};
el('all-done').onclick = async () => {
  if (!rows.length) return;
  const by = {}; for (const t of rows) (by[t.source] ||= []).push(t.id);
  try {
    for (const [src, ids] of Object.entries(by)) await api('api/update', { source: src, ids, reviewed: true });
    say(`${rows.length} marked reviewed.`, 'note', true); refresh();
  } catch (e) { say(e.message, 'error'); }
};
async function doUndo() { try { await api('api/undo', {}); say('Undone.', 'note'); refresh(); } catch (e) { say(e.message, 'error'); } }

/* ---------- spending ---------- */
async function loadSpending() { await get('api/spending?month=' + month, paintSpending); }
function paintSpending(s) {
  if (s.month !== month) return;
  el('month-label').textContent = new Date(month + '-15').toLocaleDateString(undefined, { month: 'long', year: 'numeric' });
  el('next').disabled = month >= new Date().toISOString().slice(0, 7);
  el('spent').textContent = money(s.spent);
  const ul = el('cats'); ul.replaceChildren();
  const max = Math.max(1, ...s.categories.map((c) => c.amount));
  for (const c of s.categories) {
    const li = h('li', 'mn-cat');
    const top = h('div', 'mn-cat-t'); top.append(h('span', '', c.category), h('b', c.amount < 0 ? 'pos' : '', money(c.amount)));
    const bar = h('div', 'mn-bar-bg'); const fill = h('div', 'mn-bar-fill'); fill.style.width = Math.max(0, c.amount / max * 100) + '%'; bar.append(fill);
    li.append(top, bar);
    // Open that category in Review with the filter set, so it can be changed from there.
    li.onclick = () => { tab = 'review'; f = { ...DEF, show: 'all', sort: 'spend', category: c.category, when: month }; saveF(); el('search').value = ''; showTab(); renderFilterChips(); loadList().catch((e) => say(e.message, 'error')); };
    ul.append(li);
  }
  if (!s.categories.length) ul.append(h('li', 'mn-empty', 'No spending this month yet.'));
}

/* ---------- net worth ---------- */
let nwRange = 'ytd', nwData = null;
const TAG = { bank: 'bank', manual: 'manual', import: 'import' };
async function loadNetWorth() { await get('api/networth', paintNetWorth); }
function paintNetWorth(data) {
  nwData = data;
  el('nw-total').textContent = money(nwData.net_worth);
  el('nw-total').classList.toggle('neg', nwData.net_worth < 0);
  el('nw-assets').textContent = money(nwData.assets);
  el('nw-debts').textContent = money(nwData.liabilities);
  drawChart();
  const box = el('nw-list'); box.replaceChildren();
  for (const [title, list] of [['Assets', nwData.accounts.filter((a) => a.balance > 0)], ['Owed', nwData.accounts.filter((a) => a.balance < 0)]]) {
    if (!list.length) continue;
    box.append(h('h3', 'mn-sec', title));
    const card = h('div', 'mn-bank'); const ul = h('ul', 'mn-accts');
    for (const a of list) {
      const li = h('li'); const n = h('span', 'mn-row-n');
      const sub = h('small', ''); sub.append(h('span', 'mn-tag ' + TAG[a.origin], a.origin === 'import' ? 'History' : a.origin_name), ' ', [a.detail, a.institution !== 'Manual' ? a.institution : null, a.origin === 'import' && a.as_of ? 'as of ' + day(String(a.as_of).slice(0, 10)) : null].filter(Boolean).join(' · '));
      n.append(h('b', '', a.name), sub);
      li.append(n, h('span', 'mn-amt' + (a.balance > 0 ? ' pos' : ''), money(a.balance))); ul.append(li);
    }
    card.append(ul); box.append(card);
  }
}
function drawChart() {
  const box = el('nw-chart'); box.replaceChildren();
  let pts = (nwData?.history || []).map((p) => ({ d: p.date, v: p.value }));
  const today = new Date(); const cut = nwRange === 'ytd' ? `${today.getFullYear()}-01-01` : nwRange === '90' ? new Date(today - 90 * 864e5).toISOString().slice(0, 10) : '0000';
  pts = pts.filter((p) => p.d >= cut);
  if (pts.length < 2) { box.append(h('p', 'mn-small', 'The chart fills in as days go by.')); return; }
  const W = 320, H = 110, P = 6, t0 = Date.parse(pts[0].d), t1 = Date.parse(pts[pts.length - 1].d) || t0 + 1;
  const lo = Math.min(0, ...pts.map((p) => p.v)), hi = Math.max(0, ...pts.map((p) => p.v)) || 1;
  const x = (d) => P + (Date.parse(d) - t0) / Math.max(1, t1 - t0) * (W - 2 * P);
  const y = (v) => P + (hi - v) / Math.max(1, hi - lo) * (H - 2 * P);
  const NS = 'http://www.w3.org/2000/svg', svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`); svg.setAttribute('preserveAspectRatio', 'none'); svg.setAttribute('role', 'img'); svg.setAttribute('aria-label', 'Net worth over time');
  const zero = document.createElementNS(NS, 'line'); zero.setAttribute('x1', 0); zero.setAttribute('x2', W); zero.setAttribute('y1', y(0)); zero.setAttribute('y2', y(0)); zero.setAttribute('class', 'mn-zero');
  const line = document.createElementNS(NS, 'polyline'); line.setAttribute('points', pts.map((p) => `${x(p.d).toFixed(1)},${y(p.v).toFixed(1)}`).join(' ')); line.setAttribute('class', 'mn-line');
  svg.append(zero, line); box.append(svg);
  const lab = h('div', 'mn-chart-x'); lab.append(h('span', '', day(pts[0].d)), h('span', '', day(pts[pts.length - 1].d))); box.append(lab);
}
el('nw-range').querySelectorAll('.mn-chip').forEach((b) => b.onclick = () => { nwRange = b.dataset.range; el('nw-range').querySelectorAll('.mn-chip').forEach((x) => x.classList.toggle('on', x === b)); drawChart(); });

/* ---------- accounts ---------- */
function renderAccounts() {
  const box = el('accts'); box.replaceChildren();
  const by = {};
  for (const a of ov.accounts) (by[a.institution || 'Other'] ||= []).push(a);
  for (const [inst, list] of Object.entries(by)) {
    const card = h('div', 'mn-bank'); card.append(h('b', 'mn-bank-h', inst));
    const ul = h('ul', 'mn-accts');
    for (const a of list) {
      const li = h('li', 'mn-tap'); li.tabIndex = 0;
      li.onclick = async () => {
        if (!confirm(`Leave ${a.name} out of Money? It disappears from transactions, spending and net worth. You can bring it back at the bottom of this tab.`)) return;
        try { ov = await api('api/accounts/hide', { account: a.id, hidden: true }); say(`${a.name} hidden.`, 'note'); refresh(); } catch (e) { say(e.message, 'error'); }
      };
      const n = h('span', 'mn-row-n'); n.append(h('b', '', a.name), h('small', '', a.balance_at ? 'as of ' + day(String(a.balance_at).slice(0, 10)) : ''));
      li.append(n, h('span', 'mn-amt' + ((a.balance || 0) < 0 ? '' : ' pos'), money(a.balance, a.currency))); ul.append(li);
    }
    card.append(ul); box.append(card);
  }
  if (!ov.accounts.length) box.append(h('p', 'mn-empty', ov.syncing ? 'Pulling your accounts…' : 'No accounts yet.'));
  el('sec-bank').textContent = ov.source_name;
  const mb = el('manual'); mb.replaceChildren();
  if (ov.manual.length) {
    const card = h('div', 'mn-bank'); const ul = h('ul', 'mn-accts');
    for (const m of ov.manual) {
      const li = h('li', 'mn-tap'); li.tabIndex = 0; li.onclick = () => openManual(m);
      const n = h('span', 'mn-row-n');
      const sub = m.kind === 'btc' ? `${(+m.amount).toLocaleString(undefined, { maximumFractionDigits: 8 })} BTC` + (m.btc_price ? ` at ${money(m.btc_price)}${m.price_stale ? ' (last known)' : ''}` : ' · price unavailable') : 'Dollars';
      n.append(h('b', '', m.name), h('small', '', sub + ' · updated ' + day(String(m.updated).slice(0, 10))));
      li.append(n, h('span', 'mn-amt pos', m.balance == null ? '—' : money(m.balance))); ul.append(li);
    }
    card.append(ul); mb.append(card);
  } else mb.append(h('p', 'mn-small mn-pad', 'For things no bank connection can see, like a cold-storage Bitcoin wallet.'));
  const hs = el('hist-sec'); hs.replaceChildren();
  if (ov.hidden_accounts?.length) {
    hs.append(h('h3', 'mn-sec', 'Left out of Money'));
    const card = h('div', 'mn-bank'); const ul = h('ul', 'mn-accts');
    for (const a of ov.hidden_accounts) {
      const li = h('li'); const n = h('span', 'mn-row-n'); n.append(h('b', '', a.name), h('small', '', a.institution || ''));
      const btn = h('button', 'mn-btn mn-sm', 'Bring back'); btn.type = 'button';
      btn.onclick = async () => { try { ov = await api('api/accounts/hide', { account: a.id, hidden: false }); say(`${a.name} is back.`, 'note'); refresh(); } catch (e) { say(e.message, 'error'); } };
      li.append(n, btn); ul.append(li);
    }
    card.append(ul); hs.append(card);
  }
  if (ov.history) {
    hs.append(h('h3', 'mn-sec', ''));
    hs.firstChild.append(h('span', 'mn-tag import', 'History'), ` Imported from ${ov.history.from_name}`);
    hs.append(h('p', 'mn-small mn-pad', `Read-only copy from ${day(ov.history.from)}, ${ov.history.showing.toLocaleString()} transactions showing. It stops for each account where ${ov.source_name}'s own data begins, so nothing shows twice.`));
  }
  const src = el('sources'); src.replaceChildren();
  for (const s of ov.sources) {
    const row = h('label', 'mn-src' + (s.installed ? '' : ' off'));
    const r = h('input'); r.type = 'radio'; r.name = 'src'; r.value = s.id; r.checked = s.id === ov.source; r.disabled = !s.installed;
    r.onchange = async () => { try { ov = await api('api/source', { source: s.id }); say(`Now reading ${s.name}.`, 'note'); refresh(); } catch (e) { say(e.message, 'error'); } };
    const t = h('span'); t.append(h('b', '', s.name), h('small', '', !s.installed ? 'Not installed' : s.connected ? `${s.banks} bank${s.banks === 1 ? '' : 's'} connected` : 'Installed, not connected yet'));
    row.append(r, t); src.append(row);
  }
  el('sync-note').textContent = ov.syncing ? 'Refreshing…' : `Last refreshed ${when(ov.last_sync)}${ov.error ? ' · ' + ov.error : ''}`;
  const rl = el('rules'); rl.replaceChildren();
  for (const r of ov.rules) {
    const li = h('li'); li.append(h('span', '', `“${r.match}” → ${r.category}`));
    const x = h('button', 'mn-btn mn-sm', 'Delete'); x.type = 'button';
    x.onclick = async () => { try { await api(`api/rules/${r.id}/delete`, {}); refresh(); } catch (e) { say(e.message, 'error'); } };
    li.append(x); rl.append(li);
  }
  if (!ov.rules.length) rl.append(h('li', 'mn-small', 'No rules yet. Tick “Always use this” when you categorize something.'));
}
/* ---------- manual accounts ---------- */
let mcur = null, mkind = 'btc';
function setKind(k) {
  mkind = k; el('m-kind').querySelectorAll('.mn-chip').forEach((b) => b.classList.toggle('on', b.dataset.kind === k));
  el('m-amount-lab').textContent = k === 'btc' ? 'Amount in BTC' : 'Balance in dollars';
  el('m-amount').placeholder = k === 'btc' ? '0.25' : '1000';
  el('m-hint').hidden = k !== 'btc';
}
function openManual(m) {
  mcur = m || null;
  el('m-title').textContent = m ? m.name : 'New manual account';
  el('m-name').value = m ? m.name : ''; el('m-amount').value = m ? m.amount : ''; el('m-note').value = m ? (m.note || '') : '';
  setKind(m ? m.kind : 'btc'); el('m-del').hidden = !m; el('msheet').hidden = false;
}
const closeManual = () => { el('msheet').hidden = true; mcur = null; };
el('m-kind').querySelectorAll('.mn-chip').forEach((b) => b.onclick = () => setKind(b.dataset.kind));
el('add-manual').onclick = () => openManual(null);
el('m-close').onclick = closeManual;
el('msheet').onclick = (e) => { if (e.target === el('msheet')) closeManual(); };
el('m-save').onclick = async () => {
  try {
    await api('api/manual', { id: mcur?.id, name: el('m-name').value, kind: mkind, amount: el('m-amount').value.replace(/,/g, ''), note: el('m-note').value });
    closeManual(); say('Saved.', 'note'); refresh();
  } catch (e) { say(e.message, 'error'); }
};
el('m-del').onclick = async () => {
  if (!mcur || !confirm(`Delete ${mcur.name}?`)) return;
  try { await api(`api/manual/${mcur.id}/delete`, {}); closeManual(); say('Deleted.', 'note'); refresh(); } catch (e) { say(e.message, 'error'); }
};

el('sync').onclick = async () => { try { ov = await api('api/sync', {}); say('Refreshing…', 'note'); refresh(); } catch (e) { say(e.message, 'error'); } };
el('setup-refresh').onclick = () => refresh();

/* ---------- shell ---------- */
function showTab() {
  document.querySelectorAll('[data-tab]').forEach((b) => b.classList.toggle('is-on', b.dataset.tab === tab));
  document.querySelectorAll('[data-panel]').forEach((p) => p.hidden = p.dataset.panel !== tab);
}
document.querySelectorAll('[data-tab]').forEach((b) => b.onclick = () => { tab = b.dataset.tab; showTab(); refresh(); });
el('prev').onclick = () => { const d = new Date(month + '-15'); d.setMonth(d.getMonth() - 1); month = d.toISOString().slice(0, 7); loadSpending().catch((e) => say(e.message, 'error')); };
el('next').onclick = () => { const d = new Date(month + '-15'); d.setMonth(d.getMonth() + 1); month = d.toISOString().slice(0, 7); loadSpending().catch((e) => say(e.message, 'error')); };
const LOAD = { review: () => loadList(), spending: () => loadSpending(), networth: () => loadNetWorth(), accounts: () => null };
let warmed = false;
async function refresh() {
  try {
    await Promise.all([loadOverview(), LOAD[tab]()]);
    if (!warmed) {   // fetch the other tabs in the background so they open instantly
      warmed = true;
      for (const k of Object.keys(LOAD)) if (k !== tab) LOAD[k]()?.catch?.(() => {});
    }
  } catch (e) { say(e.message, 'error'); }
}
el('search').oninput = () => { clearTimeout(timer); timer = setTimeout(() => loadList().catch((e) => say(e.message, 'error')), 250); };

window.__wizardCloseOverlay = () => {
  if (!el('fsheet').hidden) { closeFilters(); return true; }
  if (!el('msheet').hidden) { closeManual(); return true; }
  if (!el('sheet').hidden) { closeSheet(); return true; }
  return false;
};
if (!hosted) {
  history.pushState({ wzGuard: 1 }, '', location.href);
  addEventListener('popstate', () => { window.__wizardCloseOverlay(); history.pushState({ wzGuard: 1 }, '', location.href); });
}
addEventListener('keydown', (e) => { if (e.key === 'Escape') { closeFilters(); closeManual(); closeSheet(); } });
showTab(); refresh();
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
