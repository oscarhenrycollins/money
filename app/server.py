"""Money: a simple Monarch-style money app over the box's bank connections.

Reads accounts and transactions from whichever bank-connection apps are installed (SimpleFIN by default; Monarch
and Teller also work) and keeps the owner's own layer on top in its own database: reviewed state, categories,
merchant names, notes, hidden transactions and auto-category rules. The connector's data is never changed; a fresh
sync never overwrites the owner's edits.

Stdlib only. Reads connectors over the box's private app network; offers the same data to the agent through MCP.
"""
import datetime as dt
import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get('DATA_DIR', ROOT / '.data'))
APP_ID = 'money'
VERSION = '0.6.1'
PREFIX = '/' + APP_ID
PORT = os.environ.get('PORT', '8809')
SYNC_MINUTES = float(os.environ.get('MONEY_SYNC_MINUTES', '30'))
HISTORY_DAYS = int(os.environ.get('MONEY_HISTORY_DAYS', '730'))
# Known bank-connection apps, in default order. The first one installed and connected becomes the default source.
SOURCES = [s.split('=', 1) for s in os.environ.get('MONEY_SOURCES',
           'wizard-simplefin=http://wizard-simplefin:8808,wizard-monarch=http://wizard-monarch:8806,'
           'wizard-teller=http://wizard-teller:8805').split(',') if '=' in s]
NAMES = {'wizard-simplefin': 'SimpleFIN', 'wizard-monarch': 'Monarch', 'wizard-teller': 'Teller'}
CATEGORIES = ['Groceries', 'Restaurants', 'Coffee', 'Shopping', 'Travel', 'Hotels', 'Flights', 'Rides & Taxis', 'Gas',
              'Rent', 'Utilities', 'Phone & Internet', 'Subscriptions', 'Health', 'Entertainment', 'Fees', 'Gifts',
              'Business', 'Income', 'Refunds & Credits', 'Transfer', 'Card Payment', 'Other']
STATIC = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml'}
FORWARDED = ('tailscale-user-login', 'x-forwarded-for', 'forwarded', 'x-real-ip', 'cf-connecting-ip')
LOCK = threading.RLock()
SYNC_LOCK = threading.Lock()
SYNCING = threading.Event()
DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


class Problem(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message


# ---------------- storage ----------------

def _private():
    DATA.mkdir(parents=True, exist_ok=True)
    os.chmod(DATA, 0o700)
    return DATA


def db():
    c = sqlite3.connect(_private() / 'money.sqlite3', timeout=15)
    c.row_factory = sqlite3.Row
    c.executescript('''
      CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT);
      CREATE TABLE IF NOT EXISTS accounts (source TEXT, id TEXT, institution TEXT, name TEXT, last_four TEXT, type TEXT,
        currency TEXT, balance REAL, balance_at TEXT, PRIMARY KEY (source, id));
      CREATE TABLE IF NOT EXISTS tx (source TEXT, id TEXT, account TEXT, date TEXT, amount REAL, description TEXT,
        merchant TEXT, source_category TEXT, status TEXT, seen TEXT, PRIMARY KEY (source, id));
      CREATE INDEX IF NOT EXISTS tx_date ON tx(source, date);
      CREATE TABLE IF NOT EXISTS edits (source TEXT, id TEXT, reviewed INTEGER, category TEXT, merchant TEXT,
        note TEXT, hidden INTEGER, at TEXT, PRIMARY KEY (source, id));
      CREATE TABLE IF NOT EXISTS rules (id INTEGER PRIMARY KEY AUTOINCREMENT, match TEXT NOT NULL, category TEXT NOT NULL,
        merchant TEXT, created TEXT);
      CREATE TABLE IF NOT EXISTS categories (name TEXT PRIMARY KEY, pos INTEGER);
      CREATE TABLE IF NOT EXISTS log (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, source TEXT, tx TEXT, before TEXT);
      CREATE TABLE IF NOT EXISTS networth (date TEXT PRIMARY KEY, value REAL, assets REAL, liabilities REAL, origin TEXT);
      CREATE TABLE IF NOT EXISTS manual (id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, amount REAL,
        note TEXT, updated TEXT);
    ''')
    if not c.execute('SELECT 1 FROM categories LIMIT 1').fetchone():
        with c:
            c.executemany('INSERT INTO categories VALUES (?,?)', [(n, i) for i, n in enumerate(CATEGORIES)])
    os.chmod(DATA / 'money.sqlite3', 0o600)
    return c


def _now():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def _setting(c, k, default=None):
    r = c.execute('SELECT v FROM settings WHERE k=?', (k,)).fetchone()
    return json.loads(r[0]) if r else default


def _set(c, k, v):
    c.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (k, json.dumps(v)))


# ---------------- connectors ----------------

def _get(base, path, query=None, timeout=30):
    url = base.rstrip('/') + path + ('?' + urlencode(query) if query else '')
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={'Accept': 'application/json'}), timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get('error')
        except ValueError:
            msg = None
        raise Problem(502, msg or f'The bank connection answered {e.code}.') from None
    except (OSError, ValueError):
        raise Problem(502, 'Could not reach that bank connection app.') from None


_SRC = {'at': 0.0, 'list': None}
_SRC_LOCK = threading.Lock()
SRC_TTL = 90


def _probe(sid, base):
    s = {'id': sid, 'name': NAMES.get(sid, sid), 'installed': False, 'connected': False, 'banks': 0, 'last_sync': None}
    try:
        st = _get(base, '/api/status', timeout=4)
        s.update(installed=True, connected=st.get('status') == 'connected' or bool(st.get('banks')),
                 banks=len(st.get('banks') or []), last_sync=st.get('last_sync'))
    except Problem:
        pass
    return s


def sources(fresh=False):
    """Every known connector, whether it's installed (reachable) and whether it's connected to a bank.
    Checked in parallel and kept for a minute or two: an app that isn't installed takes seconds to fail its name
    lookup, and that must never sit in front of every screen tap. A background loop keeps it warm."""
    if not fresh and _SRC['list'] is not None and time.time() - _SRC['at'] < SRC_TTL:
        return [dict(x) for x in _SRC['list']]
    with _SRC_LOCK:
        if not fresh and _SRC['list'] is not None and time.time() - _SRC['at'] < SRC_TTL:
            return [dict(x) for x in _SRC['list']]
        out = [None] * len(SOURCES)

        def one(i, sid, base):
            out[i] = _probe(sid, base)
        ts = [threading.Thread(target=one, args=(i, sid, base), daemon=True) for i, (sid, base) in enumerate(SOURCES)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(8)
        out = [o or {'id': sid, 'name': NAMES.get(sid, sid), 'installed': False, 'connected': False, 'banks': 0, 'last_sync': None}
               for o, (sid, _) in zip(out, SOURCES)]
        _SRC.update(at=time.time(), list=out)
        return [dict(x) for x in out]


def _sources_loop():
    while True:
        try:
            sources(fresh=True)
        except Exception:  # noqa: BLE001 - keep the cache warm after a failed check
            pass
        time.sleep(60)


def active_source(c=None, srcs=None):
    own = c is None
    c = c or db()
    try:
        chosen = _setting(c, 'source')
    finally:
        if own:
            c.close()
    if chosen:
        return chosen
    srcs = srcs if srcs is not None else sources()
    return next((s['id'] for s in srcs if s['connected']), next((s['id'] for s in srcs if s['installed']), SOURCES[0][0]))


def _num(v):
    try:
        return round(float(v), 2) if v not in (None, '') else None
    except (TypeError, ValueError):
        return None


def sync(source=None):
    if not SYNC_LOCK.acquire(blocking=False):
        raise Problem(409, 'Already syncing.')
    SYNCING.set()
    try:
        source = source or active_source()
        base = dict(SOURCES).get(source)
        if not base:
            raise Problem(400, 'Unknown bank connection.')
        accts = _get(base, '/api/accounts').get('accounts') or []
        # Page newest -> oldest by date (connectors return at most 1000 rows per call).
        since = (dt.date.today() - dt.timedelta(days=HISTORY_DAYS)).isoformat()
        rows, to, guard = {}, None, 0
        while guard < 40:
            q = {'limit': 1000, 'from': since, **({'to': to} if to else {})}
            batch = _get(base, '/api/transactions', q, timeout=60).get('transactions') or []
            new = [t for t in batch if t['id'] not in rows]
            for t in new:
                rows[t['id']] = t
            guard += 1
            if len(batch) < 1000 or not new:
                break
            to = min(t['date'] for t in batch if t.get('date'))
        now = _now()
        with LOCK:
            c = db()
            try:
                with c:
                    c.execute('DELETE FROM accounts WHERE source=?', (source,))
                    for a in accts:
                        bal = a.get('balance') if 'balance' in a else (a.get('ledger') if a.get('ledger') is not None else a.get('available'))
                        c.execute('INSERT OR REPLACE INTO accounts VALUES (?,?,?,?,?,?,?,?,?)', (
                            source, str(a['id']), a.get('institution'), a.get('name'), a.get('last_four'),
                            a.get('type') or ('credit' if re.search(r'card|credit', str(a.get('name')), re.I) else None),
                            a.get('currency') or 'USD', _num(bal), a.get('balance_at')))
                    for t in rows.values():
                        c.execute('''INSERT INTO tx VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source,id) DO UPDATE SET
                          account=excluded.account, date=excluded.date, amount=excluded.amount, description=excluded.description,
                          merchant=excluded.merchant, source_category=excluded.source_category, status=excluded.status, seen=excluded.seen''', (
                            source, str(t['id']), str(t.get('account')), t.get('date'), _num(t.get('amount')),
                            (t.get('description') or '')[:200], (t.get('counterparty') or None), t.get('category'),
                            t.get('status') or 'posted', now))
                    # A pending charge that posted under a new id disappears from the connector: drop it here too.
                    if rows:
                        c.execute("DELETE FROM tx WHERE source=? AND status='pending' AND seen<>?", (source, now))
                    _set(c, 'last_sync', now)
                    _set(c, 'last_error', None)
            finally:
                c.close()
        return {'source': source, 'accounts': len(accts), 'transactions': len(rows)}
    except Problem as e:
        c = db()
        try:
            with c:
                _set(c, 'last_error', e.message)
        finally:
            c.close()
        raise
    finally:
        SYNCING.clear()
        SYNC_LOCK.release()


def start_sync():
    def run():
        try:
            sync()
        except Problem:
            pass
    threading.Thread(target=run, daemon=True).start()


def _scheduler():
    while True:
        try:
            c = db()
            try:
                last = _setting(c, 'last_sync')
            finally:
                c.close()
            age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.strptime(last, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=dt.timezone.utc)).total_seconds() if last else 1e9
            if age > SYNC_MINUTES * 60 and not SYNCING.is_set():
                sync()
                networth()
        except Exception:  # noqa: BLE001 - keep scheduling after a failed sync
            pass
        time.sleep(60)


# ---------------- the owner's layer ----------------

def _rules(c):
    return [dict(r) for r in c.execute('SELECT id,match,category,merchant FROM rules ORDER BY id')]


def _apply_rules(t, rules):
    text = f"{t.get('merchant') or ''} {t.get('description') or ''}".lower()
    for r in rules:
        if r['match'].lower() in text:
            return r
    return None


def _view(row, rules):
    t = dict(row)
    rule = _apply_rules(t, rules)
    t['category'] = t.pop('e_category') or (rule and rule['category']) or t.get('source_category')
    t['category_from'] = 'you' if row['e_category'] else 'rule' if rule else 'bank' if t.get('source_category') else None
    t['merchant'] = t.pop('e_merchant') or (rule and rule['merchant']) or t.get('merchant') or _tidy(t.get('description'))
    t['reviewed'] = bool(t.pop('reviewed'))
    t['hidden'] = bool(t.pop('hidden'))
    return t


def _tidy(desc):
    d = re.sub(r'\s{2,}', ' ', re.sub(r'[#*]\S+|\b\d{4,}\b', '', str(desc or ''))).strip()
    return d[:60].title() if d.isupper() else d[:60]


SELECT = '''SELECT t.source, t.id, t.account, a.institution, a.name AS account_name, a.last_four, t.date, t.amount,
  t.description, t.merchant, t.source_category, t.status, COALESCE(e.reviewed,0) AS reviewed, e.category AS e_category,
  e.merchant AS e_merchant, e.note, COALESCE(e.hidden,0) AS hidden
  FROM tx t LEFT JOIN accounts a ON a.source=t.source AND a.id=t.account LEFT JOIN edits e ON e.source=t.source AND e.id=t.id'''


def _key(inst, name, last_four):
    """Match one account across providers: bank word + last four digits (+ Morgan Stanley, which shares numbers)."""
    l4 = last_four or (re.findall(r'(\d{4,5})\)?\s*$', name or '') or [None])[0]
    if not l4:
        return None
    word = (re.findall(r'[a-z]+', (inst or '').lower()) or [''])[0]
    return (word, l4[-4:], 'morgan' in (name or '').lower())


def _live_starts(c, live):
    starts = {}
    for r in c.execute('''SELECT a.institution, a.name, a.last_four, MIN(t.date) AS d FROM tx t
                          JOIN accounts a ON a.source=t.source AND a.id=t.account WHERE t.source=? GROUP BY t.account''', (live,)):
        k = _key(r['institution'], r['name'], r['last_four'])
        if k and r['d']:
            starts[k] = min(starts.get(k, r['d']), r['d'])
    return starts


def _origin_names(c, live):
    meta = _setting(c, 'history') or {}
    return {'bank': NAMES.get(live, live), 'import': (meta.get('from_name') or 'Imported') + ' history'}


def _excluded(c):
    """Accounts the owner left out of Money, stored as account keys so imported history for them hides too."""
    return {tuple(k) for k in (_setting(c, 'excluded') or [])}


def _is_out(ex, inst, name, l4):
    k = _key(inst, name, l4)
    return bool(k) and k in ex


def set_excluded(body):
    with LOCK:
        c = db()
        try:
            src = active_source(c)
            a = c.execute('SELECT institution,name,last_four FROM accounts WHERE id=? AND source IN (?, \'import\')', (str(body.get('account') or ''), src)).fetchone()
            if not a:
                raise Problem(404, 'No such account.')
            k = _key(a['institution'], a['name'], a['last_four'])
            if not k:
                raise Problem(400, 'This account has no number to tell it apart, so it cannot be hidden.')
            ex = _excluded(c)
            ex = ex | {k} if body.get('hidden', True) else ex - {k}
            with c:
                _set(c, 'excluded', sorted(list(x) for x in ex))
        finally:
            c.close()
    return overview()


def transactions(args, c=None):
    own = c is None
    c = c or db()
    try:
        src = active_source(c)
        q, p = [SELECT + " WHERE t.source IN (?, 'import')"], [src]
        if args.get('origin') == 'bank':
            q.append('AND t.source=?'); p.append(src)
        elif args.get('origin') == 'import':
            q.append("AND t.source='import'")
        for key, op in (('from', '>='), ('to', '<=')):
            v = args.get(key)
            if v:
                if not DATE_RE.match(str(v)):
                    raise Problem(400, f'{key} must be YYYY-MM-DD.')
                q.append(f'AND t.date {op} ?'); p.append(v)
        if args.get('account'):
            v = str(args['account'])[:60]
            q.append('AND (t.account=? OR a.last_four=? OR a.name LIKE ?)'); p += [v, v, '%' + v + '%']
        if args.get('reviewed') in (True, False, 'true', 'false'):
            q.append('AND COALESCE(e.reviewed,0)=?'); p.append(1 if args['reviewed'] in (True, 'true') else 0)
        if str(args.get('hidden')) not in ('true', 'True'):
            q.append('AND COALESCE(e.hidden,0)=0')
        if args.get('min_abs') not in (None, ''):
            q.append('AND ABS(t.amount) >= ?'); p.append(abs(float(args['min_abs'])))
        for key, op in (('min_amount', '>='), ('max_amount', '<=')):
            if args.get(key) not in (None, ''):
                q.append(f'AND t.amount {op} ?'); p.append(float(args[key]))
        if args.get('direction') == 'out':
            q.append('AND t.amount < 0')
        elif args.get('direction') == 'in':
            q.append('AND t.amount > 0')
        limit = max(1, min(int(args.get('limit') or 200), 2000))
        q.append('ORDER BY t.date ASC, t.id ASC' if args.get('sort') == 'oldest' else 'ORDER BY t.date DESC, t.id DESC')
        rules = _rules(c)
        starts, names, ex = _live_starts(c, src), _origin_names(c, src), _excluded(c)
        keep = set(_setting(c, 'import_keep') or [])
        live_amounts = {}
        if keep:
            for r2 in c.execute('SELECT t.date,t.amount,a.institution,a.name,a.last_four FROM tx t JOIN accounts a ON a.source=t.source AND a.id=t.account WHERE t.source=?', (src,)):
                k2 = (_key(r2['institution'], r2['name'], r2['last_four']), round(r2['amount'] or 0, 2))
                d0 = dt.date.fromisoformat(r2['date'])
                for off in range(-4, 5):
                    live_amounts.setdefault((d0 + dt.timedelta(days=off)).isoformat(), set()).add(k2)
        rows = []
        for r in c.execute(' '.join(q), p):
            if ex and _is_out(ex, r['institution'], r['account_name'], r['last_four']):
                continue
            if r['source'] == 'import':
                k = _key(r['institution'], r['account_name'], r['last_four'])
                if k in starts and (r['date'] or '') >= starts[k]:
                    if r['id'] not in keep or (k, round(r['amount'] or 0, 2)) in live_amounts.get(r['date'], set()):
                        continue   # the live connection covers this account from here on: never show it twice
            v = _view(r, rules)
            v['origin'] = 'import' if r['source'] == 'import' else 'bank'
            v['origin_name'] = names[v['origin']]
            rows.append(v)
        if args.get('search'):
            s = str(args['search'])[:80].lower()
            rows = [r for r in rows if any(s in str(r.get(k) or '').lower() for k in ('merchant', 'description', 'note', 'category', 'account_name'))]
        if args.get('category'):
            want = str(args['category']).lower()
            rows = [r for r in rows if (r['category'] or 'Uncategorized').lower() == want]
        srt = str(args.get('sort') or '')
        base, desc = (srt[:-5], True) if srt.endswith('_desc') else (srt, False)
        text_keys = {'merchant': lambda r: re.sub(r'^\W+', '', r['merchant'] or r['description'] or '').lower(),
                     'account': lambda r: (r['account_name'] or '').lower(),
                     'category': lambda r: re.sub(r'^\W+', '', r['category']).lower() if r['category'] else chr(0xffff)}
        if base in text_keys:
            rows.sort(key=lambda r: r['date'] or '', reverse=True)
            rows.sort(key=text_keys[base], reverse=desc)
        elif srt == 'smallest':
            rows.sort(key=lambda r: abs(r['amount'] or 0))
        elif srt == 'largest':    # biggest amounts first, in or out (absolute value)
            rows.sort(key=lambda r: -abs(r['amount'] or 0))
        elif args.get('sort') == 'spend':      # costs the most first: most negative amount at the top
            rows.sort(key=lambda r: (r['amount'] if r['amount'] is not None else 0, r['date'] or ''))
        elif args.get('sort') == 'income':   # biggest money in first
            rows.sort(key=lambda r: -(r['amount'] if r['amount'] is not None else 0))
        out = {'count': len(rows[:limit]), 'total': len(rows), 'transactions': rows[:limit],
               'summary': {'out': round(sum(r['amount'] for r in rows if (r['amount'] or 0) < 0), 2),
                           'in': round(sum(r['amount'] for r in rows if (r['amount'] or 0) > 0), 2),
                           'uncategorized': sum(1 for r in rows if not r['category'])}}
        if args.get('all') is True:
            out['rows'] = rows
        return out
    finally:
        if own:
            c.close()


def update(body):
    src, tid = str(body.get('source') or ''), str(body.get('id') or '')
    ids = body.get('ids') if isinstance(body.get('ids'), list) else [tid]
    fields = {k: body[k] for k in ('reviewed', 'category', 'merchant', 'note', 'hidden') if k in body}
    if not fields:
        raise Problem(400, 'Nothing to change.')
    for k in ('category', 'merchant', 'note'):
        if k in fields and fields[k] is not None:
            fields[k] = str(fields[k]).strip()[:300 if k == 'note' else 60] or None
    with LOCK:
        c = db()
        try:
            with c:
                if 'category' in fields and fields['category'] and not c.execute('SELECT 1 FROM categories WHERE name=?', (fields['category'],)).fetchone():
                    c.execute('INSERT INTO categories VALUES (?, (SELECT COALESCE(MAX(pos),0)+1 FROM categories))', (fields['category'],))
                for i in ids[:500]:
                    i = str(i)
                    if not c.execute('SELECT 1 FROM tx WHERE source=? AND id=?', (src, i)).fetchone():
                        raise Problem(404, 'No such transaction.')
                    old = c.execute('SELECT reviewed,category,merchant,note,hidden FROM edits WHERE source=? AND id=?', (src, i)).fetchone()
                    c.execute('INSERT INTO log(at,source,tx,before) VALUES (?,?,?,?)', (_now(), src, i, json.dumps(dict(old) if old else None)))
                    cur = dict(old) if old else {'reviewed': 0, 'category': None, 'merchant': None, 'note': None, 'hidden': 0}
                    for k, v in fields.items():
                        cur[k] = (1 if v else 0) if k in ('reviewed', 'hidden') else v
                    c.execute('INSERT OR REPLACE INTO edits VALUES (?,?,?,?,?,?,?,?)', (src, i, cur['reviewed'], cur['category'], cur['merchant'], cur['note'], cur['hidden'], _now()))
                if body.get('make_rule') and fields.get('category'):
                    match = str(body.get('match') or '').strip()[:60]
                    if len(match) < 3:
                        raise Problem(400, 'A rule needs at least 3 letters to match.')
                    c.execute('INSERT INTO rules(match,category,merchant,created) VALUES (?,?,?,?)', (match, fields['category'], fields.get('merchant'), _now()))
        finally:
            c.close()
    return {'ok': True, 'changed': len(ids)}


def undo():
    with LOCK:
        c = db()
        try:
            with c:
                last = c.execute('SELECT id,at FROM log ORDER BY id DESC LIMIT 1').fetchone()
                if not last:
                    raise Problem(409, 'Nothing to undo.')
                batch = c.execute('SELECT id,source,tx,before FROM log WHERE at=? ORDER BY id DESC', (last['at'],)).fetchall()
                for r in batch:
                    b = json.loads(r['before']) if r['before'] else None
                    if b:
                        c.execute('INSERT OR REPLACE INTO edits VALUES (?,?,?,?,?,?,?,?)', (r['source'], r['tx'], b['reviewed'], b['category'], b['merchant'], b['note'], b['hidden'], _now()))
                    else:
                        c.execute('DELETE FROM edits WHERE source=? AND id=?', (r['source'], r['tx']))
                    c.execute('DELETE FROM log WHERE id=?', (r['id'],))
        finally:
            c.close()
    return {'ok': True, 'undone': len(batch)}


def rule_delete(rid):
    with LOCK:
        c = db()
        try:
            with c:
                if not c.execute('DELETE FROM rules WHERE id=?', (int(rid),)).rowcount:
                    raise Problem(404, 'No such rule.')
        finally:
            c.close()
    return {'ok': True}


def import_setup(body):
    """Bring categories and rules over from another money app: categories (ordered names) and rules
    ([{match, category, merchant?}]). Existing rules with the same match text are replaced."""
    cats = body.get('categories')
    rules = body.get('rules')
    if cats is not None and not (isinstance(cats, list) and all(isinstance(x, str) for x in cats) and len(cats) <= 500):
        raise Problem(400, 'categories must be a list of names.')
    if rules is not None and not (isinstance(rules, list) and len(rules) <= 2000):
        raise Problem(400, 'rules must be a list.')
    added = 0
    with LOCK:
        c = db()
        try:
            with c:
                if cats:
                    names = list(dict.fromkeys(x.strip()[:60] for x in cats if x.strip()))
                    if body.get('replace_categories'):
                        c.execute('DELETE FROM categories')
                    start = c.execute('SELECT COALESCE(MAX(pos),-1)+1 FROM categories').fetchone()[0]
                    for i, n in enumerate(names):
                        c.execute('INSERT OR IGNORE INTO categories VALUES (?,?)', (n, start + i))
                for r in rules or []:
                    if not isinstance(r, dict):
                        continue
                    match, cat = str(r.get('match') or '').strip()[:60], str(r.get('category') or '').strip()[:60]
                    if len(match) < 3 or not cat:
                        continue
                    c.execute('DELETE FROM rules WHERE lower(match)=lower(?)', (match,))
                    c.execute('INSERT INTO rules(match,category,merchant,created) VALUES (?,?,?,?)',
                              (match, cat, (str(r['merchant']).strip()[:60] or None) if r.get('merchant') else None, _now()))
                    c.execute('INSERT OR IGNORE INTO categories VALUES (?, (SELECT COALESCE(MAX(pos),0)+1 FROM categories))', (cat,))
                    added += 1
        finally:
            c.close()
    return {'ok': True, 'rules_added': added}


def _fetch_all(base, since):
    rows, to, guard = {}, None, 0
    while guard < 40:
        q = {'limit': 1000, 'from': since, **({'to': to} if to else {})}
        batch = _get(base, '/api/transactions', q, timeout=90).get('transactions') or []
        new = [t for t in batch if t['id'] not in rows]
        for t in new:
            rows[t['id']] = t
        guard += 1
        if len(batch) < 1000 or not new:
            break
        to = min(t['date'] for t in batch if t.get('date'))
    return rows


def import_history(body):
    """One-time copy of another money app's transactions from a start date. Rows for an account stop showing
    from the day the live connection's own data for that account begins, so nothing appears twice."""
    sid = body.get('source') or 'wizard-monarch'
    base = dict(SOURCES).get(sid)
    since = str(body.get('from') or '2026-01-01')
    if not base or not DATE_RE.match(since):
        raise Problem(400, 'Pick a money app to import from and a start date (YYYY-MM-DD).')
    with LOCK:
        c = db()
        try:
            if sid == active_source(c):
                raise Problem(400, 'That app is already your live connection.')
        finally:
            c.close()
    accts = _get(base, '/api/accounts').get('accounts') or []
    rows = _fetch_all(base, since)
    review = {str(x) for x in (body.get('review_ids') or []) if re.fullmatch(r'[A-Za-z0-9_-]{1,64}', str(x))}
    missing = review - set(rows)
    if missing:   # still waiting for review in the old app but older than the start date: bring just those
        for t in _fetch_all(base, (dt.date.fromisoformat(since) - dt.timedelta(days=730)).isoformat()).values():
            if str(t['id']) in missing:
                rows[t['id']] = t
    now = _now()
    made = 0
    with LOCK:
        c = db()
        try:
            with c:
                c.execute("DELETE FROM accounts WHERE source='import'")
                c.execute("DELETE FROM tx WHERE source='import'")
                for a in accts:
                    if (a.get('institution') or '').lower() == 'manual':
                        # The other app's hand-kept accounts become Money manual accounts (kept if already made).
                        if not c.execute('SELECT 1 FROM manual WHERE lower(name)=lower(?)', (a.get('name'),)).fetchone():
                            c.execute('INSERT INTO manual VALUES (?,?,?,?,?,?)', ('m' + str(a['id'])[-10:], str(a.get('name'))[:60], 'usd',
                                      _num(a.get('ledger') if a.get('ledger') is not None else a.get('balance')), f'Copied from {NAMES.get(sid, sid)}', now))
                            made += 1
                        continue
                    bal = a.get('ledger') if a.get('ledger') is not None else a.get('balance')
                    c.execute('INSERT OR REPLACE INTO accounts VALUES (?,?,?,?,?,?,?,?,?)', ('import', str(a['id']), a.get('institution'),
                              a.get('name'), a.get('last_four'), a.get('type'), a.get('currency') or 'USD', _num(bal), a.get('balance_at')))
                for t in rows.values():
                    c.execute('INSERT OR REPLACE INTO tx VALUES (?,?,?,?,?,?,?,?,?,?)', ('import', str(t['id']), str(t.get('account')), t.get('date'),
                              _num(t.get('amount')), (t.get('description') or '')[:200], t.get('counterparty') or None, t.get('category'), 'posted', now))
                    # History arrives already reviewed: it was reviewed (or not) in the old app, not here.
                    pend = str(t['id']) in review
                    c.execute('INSERT OR IGNORE INTO edits VALUES (?,?,?,?,?,?,?,?)', ('import', str(t['id']), 0 if pend else 1, None, None, None, 0, now))
                    if pend:
                        c.execute("UPDATE edits SET reviewed=0 WHERE source='import' AND id=?", (str(t['id']),))
                _set(c, 'import_keep', sorted(review))
                try:   # monthly net worth history, if the other app keeps one
                    snaps = (_get(base, '/api/snapshot', {'kind': 'net_worth'}).get('data') or {}).get('snapshots') or []
                except Problem:
                    snaps = []
                c.execute("DELETE FROM networth WHERE origin='import'")
                for sn in snaps:
                    if DATE_RE.match(str(sn.get('date'))) and sn['date'] >= since[:8] + '01' and sn.get('net_worth') is not None:
                        c.execute("INSERT OR IGNORE INTO networth VALUES (?,?,?,?,'import')", (sn['date'], _num(sn['net_worth']),
                                  _num(sn.get('assets_balance')), _num(sn.get('liabilities_balance'))))
                _set(c, 'history', {'from': since, 'from_name': NAMES.get(sid, sid), 'source': sid, 'at': now, 'rows': len(rows)})
        finally:
            c.close()
    shown = transactions({'origin': 'import', 'limit': 1, 'hidden': 'true'})['total']
    return {'ok': True, 'imported': len(rows), 'showing': shown, 'manual_accounts_added': made}


def _btc_price(c):
    cached = _setting(c, 'btc') or {}
    if cached.get('at') and time.time() - cached['at'] < 600:
        return cached
    try:
        req = urllib.request.Request('https://api.coinbase.com/v2/prices/BTC-USD/spot', headers={'User-Agent': f'money/{VERSION}'})
        with urllib.request.urlopen(req, timeout=8) as r:
            price = float(json.loads(r.read())['data']['amount'])
        cached = {'price': round(price, 2), 'at': time.time()}
        with c:
            _set(c, 'btc', cached)
    except (OSError, ValueError, KeyError):
        cached['stale'] = True
    return cached


def manual_accounts(c):
    rows = [dict(r) for r in c.execute('SELECT id,name,kind,amount,note,updated FROM manual ORDER BY name')]
    price = _btc_price(c) if any(r['kind'] == 'btc' for r in rows) else {}
    for r in rows:
        r['balance'] = round((r['amount'] or 0) * price['price'], 2) if r['kind'] == 'btc' and price.get('price') else (r['amount'] if r['kind'] != 'btc' else None)
        if r['kind'] == 'btc':
            r['btc_price'] = price.get('price')
            r['price_stale'] = bool(price.get('stale'))
    return rows


def manual_save(body):
    name = str(body.get('name') or '').strip()[:60]
    kind = body.get('kind')
    if not name:
        raise Problem(400, 'Give the account a name.')
    if kind not in ('btc', 'usd'):
        raise Problem(400, 'Pick Bitcoin or Dollars.')
    try:
        amount = float(body.get('amount'))
    except (TypeError, ValueError):
        raise Problem(400, 'Enter an amount.') from None
    if not -1e12 < amount < 1e12 or (kind == 'btc' and not 0 <= amount <= 21e6):
        raise Problem(400, 'That amount looks wrong.')
    mid = str(body.get('id') or '') or 'm' + secrets_token()
    with LOCK:
        c = db()
        try:
            with c:
                c.execute('INSERT OR REPLACE INTO manual VALUES (?,?,?,?,?,?)', (mid, name, kind, round(amount, 8),
                          (str(body.get('note') or '').strip()[:200] or None), _now()))
        finally:
            c.close()
    return {'ok': True, 'id': mid}


def secrets_token():
    import secrets
    return secrets.token_hex(5)


def manual_delete(mid):
    with LOCK:
        c = db()
        try:
            with c:
                if not c.execute('DELETE FROM manual WHERE id=?', (mid,)).rowcount:
                    raise Problem(404, 'No such account.')
        finally:
            c.close()
    return {'ok': True}


def networth():
    """Everything owned minus owed: live bank accounts, then imported balances for accounts the live connection
    doesn't have yet, then manual accounts. Records one point per day for the history chart."""
    c = db()
    try:
        src = active_source(c)
        names = _origin_names(c, src)
        rows = []
        ex = _excluded(c)
        live = [dict(r) for r in c.execute('SELECT institution,name,last_four,type,balance,balance_at FROM accounts WHERE source=?', (src,))
                if not _is_out(ex, r['institution'], r['name'], r['last_four'])]
        keys = {_key(a['institution'], a['name'], a['last_four']) for a in live}
        for a in live:
            if a['balance'] is not None:
                rows.append({'name': a['name'], 'institution': a['institution'], 'origin': 'bank', 'origin_name': names['bank'], 'balance': a['balance'], 'as_of': a['balance_at']})
        hist = _setting(c, 'history') or {}
        for a in c.execute("SELECT institution,name,last_four,balance,balance_at FROM accounts WHERE source='import' AND balance IS NOT NULL"):
            if _key(a['institution'], a['name'], a['last_four']) in keys or _is_out(ex, a['institution'], a['name'], a['last_four']):
                continue
            if abs(a['balance']) < 0.005:
                continue
            rows.append({'name': a['name'], 'institution': a['institution'], 'origin': 'import', 'origin_name': names['import'],
                         'balance': a['balance'], 'as_of': a['balance_at'] or hist.get('at')})
        for m in manual_accounts(c):
            if m['balance'] is not None:
                rows.append({'name': m['name'], 'institution': 'Manual', 'origin': 'manual', 'origin_name': 'Manual', 'balance': m['balance'], 'as_of': m['updated'],
                             'detail': f"{m['amount']:g} BTC" if m['kind'] == 'btc' else None})
        assets = round(sum(r['balance'] for r in rows if r['balance'] > 0), 2)
        debts = round(sum(r['balance'] for r in rows if r['balance'] < 0), 2)
        total = round(assets + debts, 2)
        if rows:
            with c:
                c.execute("INSERT OR REPLACE INTO networth VALUES (?,?,?,?,'money')", (dt.date.today().isoformat(), total, assets, debts))
        points = [dict(r) for r in c.execute('SELECT date,value,origin FROM networth ORDER BY date')]
    finally:
        c.close()
    rows.sort(key=lambda r: -abs(r['balance']))
    return {'net_worth': total, 'assets': assets, 'liabilities': debts, 'accounts': rows, 'history': points,
            'note': 'Negative balances are money owed (cards, loans). Imported balances are as of the import and drop out once the live connection has that account.'}


def set_source(body):
    sid = body.get('source')
    if sid not in dict(SOURCES):
        raise Problem(400, 'Unknown bank connection.')
    with LOCK:
        c = db()
        try:
            with c:
                _set(c, 'source', sid)
                _set(c, 'last_sync', None)
        finally:
            c.close()
    _SRC['at'] = 0.0
    start_sync()
    return overview()


# ---------------- summaries ----------------

def _month_bounds(month):
    if month and not re.match(r'^\d{4}-\d{2}$', str(month)):
        raise Problem(400, 'month must be YYYY-MM.')
    d = dt.date.fromisoformat((month or dt.date.today().strftime('%Y-%m')) + '-01')
    nxt = (d + dt.timedelta(days=32)).replace(day=1)
    return d.isoformat(), (nxt - dt.timedelta(days=1)).isoformat(), d.strftime('%Y-%m')


SKIP = {'transfer', 'card payment', 'credit card payment'}


def spending(month=None):
    start, end, label = _month_bounds(month)
    rows = transactions({'from': start, 'to': end, 'limit': 2000})['transactions']
    cats, income, spent = {}, 0.0, 0.0
    for t in rows:
        cat = t['category'] or 'Uncategorized'
        if cat.lower() in SKIP or t['amount'] is None:
            continue
        if t['amount'] > 0 and cat.lower() in ('income', 'business income', 'other income', 'va income'):
            income += t['amount']
            continue
        cats[cat] = cats.get(cat, 0) - t['amount']   # refunds and credits net against spending
    cats = {k: round(v, 2) for k, v in cats.items() if abs(v) >= 0.005}
    spent = round(sum(v for v in cats.values() if v > 0), 2)
    return {'month': label, 'spent': spent, 'income': round(income, 2),
            'categories': sorted(({'category': k, 'amount': v} for k, v in cats.items()), key=lambda x: -x['amount'])}


def overview():
    srcs = sources()
    c = db()
    try:
        src = active_source(c, srcs)
        ex = _excluded(c)
        allaccts = [dict(r) for r in c.execute('SELECT id,institution,name,last_four,type,currency,balance,balance_at FROM accounts WHERE source=? ORDER BY institution,name', (src,))]
        accts = [a for a in allaccts if not _is_out(ex, a['institution'], a['name'], a['last_four'])]
        hidden_accts = [a for a in allaccts if _is_out(ex, a['institution'], a['name'], a['last_four'])]
        visible = transactions({'limit': 1, 'all': True}, c)
        allrows = visible['rows']
        cats = [r[0] for r in c.execute('SELECT name FROM categories ORDER BY pos')]
        hist = _setting(c, 'history')
        if hist:
            hist['showing'] = sum(1 for r in allrows if r['origin'] == 'import')
        out = {'source': src, 'source_name': NAMES.get(src, src), 'sources': srcs, 'chosen': bool(_setting(c, 'source')),
               'accounts': accts, 'hidden_accounts': hidden_accts, 'manual': manual_accounts(c), 'history': hist,
               'to_review': sum(1 for r in allrows if not r['reviewed']), 'transactions': len(allrows),
               'categories': cats, 'rules': _rules(c),
               'last_sync': _setting(c, 'last_sync'), 'error': _setting(c, 'last_error'), 'syncing': SYNCING.is_set()}
    finally:
        c.close()
    cash = sum(a['balance'] or 0 for a in accts if (a['type'] or '') in ('depository', 'checking', 'savings') or re.search(r'check|saving|ckg', (a['name'] or ''), re.I))
    out['cash'] = round(cash, 2)
    return out


# ---------------- MCP ----------------

TOOLS = [
    {'name': 'money_overview', 'description': "Money app overview: which bank connection it reads (SimpleFIN by default), its accounts with balances, manual accounts (e.g. Bitcoin cold storage), imported history, how many transactions still need review, categories and auto-category rules, last sync.",
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}, 'annotations': {'readOnlyHint': True}},
    {'name': 'money_transactions', 'description': "Search transactions with the owner's categories, merchant names, notes and reviewed state applied. Filters: from/to (YYYY-MM-DD), account (id, last digits or name), search, category, reviewed (true/false), min_amount/max_amount, direction (out = money out, in = money in), origin (bank = live connection, import = imported history), min_abs (size at least, in or out), sort (newest default, oldest, largest = biggest amounts first in or out, smallest, merchant/account/category A-Z or *_desc Z-A, spend = costs the most first, income = biggest money in first), limit (default 200). Each row says its origin. Negative = money out.",
     'inputSchema': {'type': 'object', 'properties': {'from': {'type': 'string'}, 'to': {'type': 'string'}, 'account': {'type': 'string'}, 'search': {'type': 'string'}, 'category': {'type': 'string'},
                     'reviewed': {'type': 'boolean'}, 'min_amount': {'type': 'number'}, 'max_amount': {'type': 'number'}, 'limit': {'type': 'integer'},
                     'direction': {'type': 'string', 'enum': ['out', 'in']}, 'min_abs': {'type': 'number'}, 'origin': {'type': 'string', 'enum': ['bank', 'import']},
                     'sort': {'type': 'string', 'enum': ['newest', 'oldest', 'largest', 'smallest', 'merchant', 'merchant_desc', 'account', 'account_desc', 'category', 'category_desc', 'spend', 'income']}}, 'additionalProperties': False},
     'annotations': {'readOnlyHint': True}},
    {'name': 'money_spending', 'description': 'Spending by category for a month (YYYY-MM, default this month); refunds and credits net against their category; transfers and card payments excluded.',
     'inputSchema': {'type': 'object', 'properties': {'month': {'type': 'string'}}, 'additionalProperties': False}, 'annotations': {'readOnlyHint': True}},
    {'name': 'money_update', 'description': "Change the owner's layer on transactions (never the bank's data): reviewed, category, merchant, note, hidden. Pass source and id (or ids for several). With make_rule true and match text, also saves a rule so future matching transactions get that category. Only do this when the owner asked.",
     'inputSchema': {'type': 'object', 'properties': {'source': {'type': 'string'}, 'id': {'type': 'string'}, 'ids': {'type': 'array', 'items': {'type': 'string'}}, 'reviewed': {'type': 'boolean'},
                     'category': {'type': 'string'}, 'merchant': {'type': 'string'}, 'note': {'type': 'string'}, 'hidden': {'type': 'boolean'}, 'make_rule': {'type': 'boolean'}, 'match': {'type': 'string'}},
                     'required': ['source'], 'additionalProperties': False},
     'annotations': {'readOnlyHint': False, 'destructiveHint': False}},
    {'name': 'money_net_worth', 'description': 'Net worth now (assets, liabilities, every account with where its balance comes from: bank connection, manual, or imported history) and its history (monthly imported points plus one point per day since).',
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}, 'annotations': {'readOnlyHint': True}},
    {'name': 'money_manual_account', 'description': "Add or update one of the owner's manual accounts (e.g. a cold-storage Bitcoin wallet): name, kind ('btc' = amount in BTC, valued at the live price; 'usd' = dollar balance), amount, optional note, id to update. Only when the owner asks; never ask for seed phrases or keys.",
     'inputSchema': {'type': 'object', 'properties': {'id': {'type': 'string'}, 'name': {'type': 'string'}, 'kind': {'type': 'string', 'enum': ['btc', 'usd']}, 'amount': {'type': 'number'}, 'note': {'type': 'string'}},
                     'required': ['name', 'kind', 'amount'], 'additionalProperties': False},
     'annotations': {'readOnlyHint': False, 'destructiveHint': False}},
]


def rpc(msg):
    if not isinstance(msg, dict):
        return {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Expected a JSON-RPC object.'}}
    m, i, p = msg.get('method'), msg.get('id'), msg.get('params') or {}
    if 'id' not in msg:
        return None
    try:
        if m == 'initialize':
            r = {'protocolVersion': '2025-06-18', 'capabilities': {'tools': {}}, 'serverInfo': {'name': APP_ID, 'version': VERSION}}
        elif m == 'tools/list':
            r = {'tools': TOOLS}
        elif m == 'tools/call':
            a, n = p.get('arguments') or {}, p.get('name')
            fn = {'money_overview': lambda: overview(), 'money_transactions': lambda: transactions(a),
                  'money_spending': lambda: spending(a.get('month')), 'money_update': lambda: update(a),
                  'money_manual_account': lambda: manual_save(a), 'money_net_worth': lambda: networth()}.get(n)
            if not fn:
                return {'jsonrpc': '2.0', 'id': i, 'error': {'code': -32602, 'message': 'Unknown tool.'}}
            v = fn()
            r = {'content': [{'type': 'text', 'text': json.dumps(v, ensure_ascii=False)}], 'structuredContent': v, 'isError': False}
        elif m == 'ping':
            r = {}
        else:
            return {'jsonrpc': '2.0', 'id': i, 'error': {'code': -32601, 'message': 'Unknown method.'}}
    except Problem as e:
        r = {'content': [{'type': 'text', 'text': e.message}], 'isError': True}
    except (ValueError, TypeError):
        r = {'content': [{'type': 'text', 'text': 'Invalid arguments.'}], 'isError': True}
    return {'jsonrpc': '2.0', 'id': i, 'result': r}


# ---------------- HTTP ----------------

def _csv(name):
    return {x.strip().lower() for x in os.environ.get(name, '').split(',') if x.strip()}


_LOOPBACK = {f'127.0.0.1:{PORT}', f'localhost:{PORT}', f'{APP_ID}:{PORT}'}
TRUSTED_HOSTS = _csv('WIZARD_TRUSTED_HOSTS') | _LOOPBACK
TRUSTED_ORIGINS = _csv('WIZARD_TRUSTED_ORIGINS') | {'http://' + h for h in _LOOPBACK}
CSP = "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'self'; frame-ancestors 'self'"


class Handler(BaseHTTPRequestHandler):
    def refused(self, write=False):
        if (self.headers.get('Host') or '').lower() not in TRUSTED_HOSTS:
            self.send(403, {'error': 'Unknown host.'}); return True
        origin = (self.headers.get('Origin') or '').lower()
        if origin and origin not in TRUSTED_ORIGINS:
            self.send(403, {'error': 'Cross-origin request refused.'}); return True
        if write and not origin and (self.headers.get('Sec-Fetch-Site') or '') == 'cross-site':
            self.send(403, {'error': 'Cross-origin request refused.'}); return True
        return False

    def send(self, code, body=None, kind='application/json'):
        b = body if isinstance(body, bytes) else b'' if body is None else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(b)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', CSP)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(b)

    def route(self):
        p = urlsplit(self.path).path
        return '/' if p == PREFIX else p[len(PREFIX):] if p.startswith(PREFIX + '/') else p

    def body(self):
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            raise Problem(415, 'Use application/json.')
        size = int(self.headers.get('Content-Length', '0') or 0)
        if not 0 <= size <= 262144:
            raise Problem(413, 'Request too large.')
        try:
            d = json.loads(self.rfile.read(size) or b'{}')
        except ValueError:
            raise Problem(400, 'Invalid JSON.') from None
        if not isinstance(d, dict):
            raise Problem(400, 'Expected a JSON object.')
        return d

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if self.refused():
            return
        path, q = self.route(), {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
        try:
            if path in ('/healthz', '/api/health'):
                return self.send(200, {'ok': True, 'app': APP_ID, 'version': VERSION})
            if path == '/api/overview':
                return self.send(200, overview())
            if path == '/api/transactions':
                return self.send(200, transactions(q))
            if path == '/api/networth':
                return self.send(200, networth())
            if path == '/api/spending':
                return self.send(200, spending(q.get('month')))
            if path == '/openapi.json':
                return self.send(200, (ROOT / 'openapi.json').read_bytes())
            name = 'index.html' if path == '/' else path[1:]
            kind = STATIC.get(Path(name).suffix)
            if kind and '/' not in name and not name.startswith('.') and (ROOT / 'web' / name).is_file():
                return self.send(200, (ROOT / 'web' / name).read_bytes(), kind)
            self.send(405 if path == '/mcp' else 404, {'error': 'Use POST /mcp.' if path == '/mcp' else 'Not found.'})
        except Problem as e:
            self.send(e.code, {'error': e.message})
        except (ValueError, TypeError):
            self.send(400, {'error': 'Invalid query.'})

    def do_POST(self):
        if self.refused(write=True):
            return
        path = self.route()
        try:
            if path == '/mcp':
                if any(self.headers.get(h) for h in FORWARDED):
                    return self.send(403, {'error': 'MCP is box-only.'})
                out = rpc(self.body())
                return self.send(202 if out is None else 200, out)
            b = self.body()
            if path == '/api/update':
                return self.send(200, update(b))
            if path == '/api/undo':
                return self.send(200, undo())
            if path == '/api/accounts/hide':
                return self.send(200, set_excluded(b))
            if path == '/api/import/history':
                return self.send(200, import_history(b))
            if path == '/api/manual':
                return self.send(200, manual_save(b))
            m = re.fullmatch(r'/api/manual/([A-Za-z0-9]{1,20})/delete', path)
            if m:
                return self.send(200, manual_delete(m.group(1)))
            if path == '/api/import':
                return self.send(200, import_setup(b))
            if path == '/api/source':
                return self.send(200, set_source(b))
            if path == '/api/sync':
                if not SYNCING.is_set():
                    start_sync()
                time.sleep(0.2)
                return self.send(202, overview())
            m = re.fullmatch(r'/api/rules/(\d{1,9})/delete', path)
            if m:
                return self.send(200, rule_delete(m.group(1)))
            self.send(404, {'error': 'Not found.'})
        except Problem as e:
            self.send(e.code, {'error': e.message})
        except (ValueError, TypeError):
            self.send(400, {'error': 'Invalid request.'})

    def log_message(self, *a):
        pass


if __name__ == '__main__':
    os.umask(0o077)
    for f in _private().rglob('*'):
        try:
            os.chmod(f, 0o700 if f.is_dir() else 0o600)
        except OSError:
            pass
    db().close()
    threading.Thread(target=_sources_loop, daemon=True).start()
    threading.Thread(target=_scheduler, daemon=True).start()
    ThreadingHTTPServer((os.environ.get('BIND', '0.0.0.0'), int(PORT)), Handler).serve_forever()
