"""SQLite ledger and conservative reconciliation."""
import hashlib
import json
import sqlite3
import threading
import uuid
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from importer import clean, cents, timestamp, normalize, read_table

CATEGORIES = ['餐饮', '购物', '交通', '住房', '学习', '娱乐', '医疗', '手续费', '工资／实习', '生活费', '奖学金', '其他']
KINDS = {'income': '收入', 'expense': '支出', 'refund': '退款', 'transfer': '账户转移', 'duplicate': '重复记录', 'ignored': '不计收支', 'unknown': '待确定收支'}
CHANNELS = ['微信', '支付宝', '银行卡', '手工']
TRANSFER_WORDS = ['提现', '充值', '信用卡还款', '账户互转', '转账']


class Ledger:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.raw_dir = self.root / 'originals'
        self.raw_dir.mkdir(exist_ok=True)
        self.path = self.root / 'ledger.sqlite3'
        self.lock = threading.RLock()
        with self.connection() as con:
            con.executescript("""
              CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
              INSERT OR IGNORE INTO meta VALUES('schema','1');
              CREATE TABLE IF NOT EXISTS accounts(id INTEGER PRIMARY KEY,name TEXT UNIQUE NOT NULL,aliases TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS imports(id TEXT PRIMARY KEY,name TEXT,channel TEXT,account_id INTEGER,created TEXT,raw_file TEXT,report TEXT);
              CREATE TABLE IF NOT EXISTS transactions(
                id INTEGER PRIMARY KEY,identity TEXT UNIQUE NOT NULL,channel TEXT,account_id INTEGER,
                time TEXT,cents INTEGER,base_kind TEXT,kind TEXT,direction TEXT,merchant TEXT,description TEXT,
                tx_type TEXT,status TEXT,funding TEXT,external_id TEXT,merchant_id TEXT,category TEXT,review TEXT,
                initial_review TEXT,note TEXT DEFAULT '',manual INTEGER DEFAULT 0,category_manual INTEGER DEFAULT 0,
                link_id INTEGER,link_reason TEXT DEFAULT '',import_id TEXT,source_row INTEGER,raw TEXT);
              CREATE TABLE IF NOT EXISTS sources(tx_id INTEGER,import_id TEXT,row_number INTEGER,raw TEXT,UNIQUE(tx_id,import_id,row_number));
              CREATE TABLE IF NOT EXISTS rules(id INTEGER PRIMARY KEY,keyword TEXT UNIQUE NOT NULL,category TEXT NOT NULL);
            """)
            for name, aliases in [('微信零钱', ['微信零钱', '微信余额']), ('支付宝余额', ['支付宝余额'])]:
                con.execute('INSERT OR IGNORE INTO accounts(name,aliases) VALUES(?,?)', (name, json.dumps(aliases, ensure_ascii=False)))

    @contextmanager
    def connection(self):
        with self.lock:
            con = sqlite3.connect(self.path, timeout=15)
            con.row_factory = sqlite3.Row
            try:
                yield con
                con.commit()
            except Exception:
                con.rollback()
                raise
            finally:
                con.close()

    def accounts(self, con):
        return [dict(row, aliases=json.loads(row['aliases'])) for row in con.execute('SELECT * FROM accounts ORDER BY id')]

    def account(self, name, aliases):
        name = clean(name)
        if not name or len(name) > 80:
            raise ValueError('请输入 1–80 字的账户名')
        items = list(dict.fromkeys([name] + [clean(a) for a in aliases if clean(a)]))
        if any(len(a) < 3 or a in {'微信', '支付宝', '银行', '余额', '零钱', '转账', '充值', '提现'} for a in items):
            raise ValueError('别名应至少 3 字，使用银行和尾号，避免“银行”等泛称')
        with self.connection() as con:
            for account in self.accounts(con):
                if any(a in account['aliases'] for a in items):
                    raise ValueError('账户名或别名已被其他账户使用')
            con.execute('INSERT INTO accounts(name,aliases) VALUES(?,?)', (name, json.dumps(items, ensure_ascii=False)))
            self.reconcile(con)

    @staticmethod
    def account_matches(text, accounts):
        return {account['id'] for account in accounts if any(alias and alias in text for alias in account['aliases'])}

    def import_file(self, name, blob, channel, account_id, mapping=None, header_row=None, sheet=None, password=None):
        if channel not in CHANNELS:
            raise ValueError('未知渠道')
        table = read_table(name, blob, header_row, sheet, password)
        mapping = mapping or table['mapping']
        if not mapping.get('time') or not (mapping.get('amount') or (mapping.get('income_amount') and mapping.get('expense_amount'))):
            raise ValueError('必须映射日期和金额，或日期、收入金额、支出金额')
        if any(value not in table['headers'] for value in mapping.values() if value):
            raise ValueError('映射列不存在')
        digest = hashlib.sha256(blob).hexdigest()
        import_id = hashlib.sha256(json.dumps([digest, channel, account_id, mapping, table['header_row'], table['sheet']], sort_keys=True).encode()).hexdigest()
        report = {'new': 0, 'duplicate': 0, 'errors': [], 'rows': len(table['records']), 'warnings': table.get('warnings', [])}
        with self.connection() as con:
            if not con.execute('SELECT 1 FROM accounts WHERE id=?', (account_id,)).fetchone():
                raise ValueError('请先建立并选择自己的账户')
            old = con.execute('SELECT report FROM imports WHERE id=?', (import_id,)).fetchone()
            if old:
                return dict(json.loads(old['report']), repeated_file=True, new=0)
            raw_file = digest + Path(name).suffix.lower()
            target = self.raw_dir / raw_file
            if not target.exists():
                with target.open('xb') as stream:
                    stream.write(blob)
            con.execute('INSERT INTO imports VALUES(?,?,?,?,?,?,?)', (import_id, Path(name).name, channel, account_id, datetime.now().isoformat(timespec='seconds'), raw_file, '{}'))
            occurrences = Counter()
            for row_number, row in table['records']:
                try:
                    tx = normalize(row, mapping)
                except (ValueError, TypeError) as exc:
                    report['errors'].append({'row': row_number, 'error': str(exc)})
                    continue
                serialized = json.dumps(row, ensure_ascii=False, sort_keys=True)
                event = [channel, account_id, tx['external_id'], tx['time'], tx['cents'], tx['base_kind'], tx['tx_type']]
                if not tx['external_id']:
                    event += [tx['merchant'], tx['description'], tx['funding'], tx['status']]
                    key = json.dumps(event, ensure_ascii=False)
                    occurrences[key] += 1
                    event.append(occurrences[key])
                identity = hashlib.sha256(json.dumps(event, ensure_ascii=False).encode()).hexdigest()
                existing = con.execute('SELECT id FROM transactions WHERE identity=?', (identity,)).fetchone()
                if existing:
                    tx_id = existing['id']
                    report['duplicate'] += 1
                else:
                    tx.update(identity=identity, channel=channel, account_id=account_id, import_id=import_id, source_row=row_number, raw=serialized)
                    columns = list(tx)
                    cursor = con.execute(f"INSERT INTO transactions({','.join(columns)}) VALUES({','.join('?' for _ in columns)})", [tx[key] for key in columns])
                    tx_id = cursor.lastrowid
                    report['new'] += 1
                con.execute('INSERT OR IGNORE INTO sources VALUES(?,?,?,?)', (tx_id, import_id, row_number, serialized))
            self.reconcile(con)
            report['pending'] = con.execute("SELECT count(*) FROM transactions WHERE review<>''").fetchone()[0]
            con.execute('UPDATE imports SET report=? WHERE id=?', (json.dumps(report, ensure_ascii=False), import_id))
        return report

    def reconcile(self, con):
        accounts = self.accounts(con)
        rules = [dict(row) for row in con.execute('SELECT * FROM rules ORDER BY id')]
        rows = [dict(row) for row in con.execute('SELECT * FROM transactions ORDER BY time,id')]
        buckets = defaultdict(list)
        for row in rows:
            if not row['manual']:
                row.update(kind=row['base_kind'], link_id=None, link_reason='', review=row['initial_review'])
                transfer_text = row['tx_type'] + ' ' + row['description']
                # Funding from one's own card does not make a transfer to a friend internal.
                generic_transfer = '转账' in row['tx_type'] and not any(w in row['tx_type'] for w in TRANSFER_WORDS[:-1])
                text = ' '.join([row['merchant'], row['description']]) if generic_transfer else ' '.join([row['merchant'], transfer_text, row['funding']])
                owned_targets = self.account_matches(text, accounts) - {row['account_id']}
                fee = any(word in transfer_text for word in ['手续费', '服务费'])
                transfer_hint = any(word in row['tx_type'] for word in TRANSFER_WORDS)
                if not row['tx_type']:
                    transfer_hint = any(word in row['description'] for word in ['微信零钱充值', '微信零钱提现', '支付宝余额充值', '信用卡还款', '账户互转'])
                if row['base_kind'] not in {'ignored', 'refund'} and not fee and transfer_hint:
                    if len(owned_targets) == 1:
                        row.update(kind='transfer', review='', link_reason='转移描述指向已登记的本人账户；另一端未导入也不计收支')
                    elif any(word in transfer_text for word in TRANSFER_WORDS[:-1]):
                        row['review'] = '疑似账户转移，请确认接收账户是否属于自己'
            if not row['category_manual']:
                row['category'] = '手续费' if any(w in row['tx_type'] + row['description'] for w in ['手续费', '服务费']) else '其他'
                for rule in rules:
                    if rule['keyword'].casefold() in (row['merchant'] + ' ' + row['description']).casefold():
                        row['category'] = rule['category']
                        break
            row['_targets'] = self.account_matches(' '.join([row['merchant'], row['description'], row['funding']]), accounts)
            row['_dt'] = datetime.fromisoformat(row['time'])
            buckets[row['cents']].append(row)
        pairs = defaultdict(list)
        for group in buckets.values():
            for i, left in enumerate(group):
                if left['manual'] or left['kind'] in {'ignored', 'duplicate'}:
                    continue
                for right in group[i+1:]:
                    if right['manual'] or right['kind'] in {'ignored', 'duplicate'} or left['account_id'] == right['account_id']:
                        continue
                    gap = abs((left['_dt'] - right['_dt']).total_seconds())
                    if gap > 72*3600:
                        continue
                    l_to_r = right['account_id'] in left['_targets']
                    r_to_l = left['account_id'] in right['_targets']
                    reference = set(filter(None, [left['external_id'], left['merchant_id']])) & set(filter(None, [right['external_id'], right['merchant_id']]))
                    opposite = (left['base_kind'], right['base_kind']) in {('income', 'expense'), ('expense', 'income')}
                    transfer = (left['kind'] == 'transfer' or right['kind'] == 'transfer') and (opposite or 'unknown' in {left['base_kind'], right['base_kind']})
                    kind = None
                    if transfer and ((l_to_r and r_to_l) or (reference and (l_to_r or r_to_l))):
                        kind = 'transfer'
                    bank, wallet = (left, right) if left['channel'] == '银行卡' else (right, left)
                    payment_hint = any(w in bank['description'] + bank['tx_type'] + bank['merchant'] for w in ['微信支付', '财付通', '支付宝', '快捷支付', '消费'])
                    consumption = left['kind'] == right['kind'] and left['kind'] in {'expense', 'refund'} and wallet['channel'] in {'微信', '支付宝'} and bank['channel'] == '银行卡' and gap <= 120 and (l_to_r or r_to_l) and payment_hint
                    if consumption and reference:
                        kind = 'duplicate'
                    elif consumption:
                        left['review'] = right['review'] = '疑似同一交易的渠道与银行卡记录，请关联；当前两条均计入'
                    if kind:
                        pairs[left['id']].append((right, kind))
                        pairs[right['id']].append((left, kind))
        for left in rows:
            candidates = pairs[left['id']]
            if left['link_id'] or left['manual']:
                continue
            if len(candidates) != 1:
                if len(candidates) > 1:
                    left['review'] = '检测到多个可能对应的记录，请手动关联'
                continue
            right, kind = candidates[0]
            if len(pairs[right['id']]) != 1:
                left['review'] = '检测到多个可能对应的记录，请手动关联'
                continue
            left['link_id'], right['link_id'] = right['id'], left['id']
            left['review'] = right['review'] = ''
            if kind == 'transfer':
                left['kind'] = right['kind'] = 'transfer'
                left['link_reason'] = right['link_reason'] = '本人账户、金额、到账时间及双方描述／订单引用匹配'
            else:
                bank = left if left['channel'] == '银行卡' else right
                bank['kind'] = 'duplicate'
                left['link_reason'] = right['link_reason'] = '订单引用、金额、资金账户及时间匹配，只保留支付渠道记录'
        by_id = {row['id']: row for row in rows}
        for row in rows:
            if row['kind'] == 'refund' and row['link_id'] and not row['category_manual']:
                purchase = by_id.get(row['link_id'])
                if purchase and purchase['kind'] == 'expense':
                    row['category'] = purchase['category']
        for row in rows:
            con.execute('UPDATE transactions SET kind=?,category=?,review=?,link_id=?,link_reason=? WHERE id=?', (row['kind'], row['category'], row['review'], row['link_id'], row['link_reason'], row['id']))

    def edit(self, tx_id, changes):
        with self.connection() as con:
            row = con.execute('SELECT * FROM transactions WHERE id=?', (tx_id,)).fetchone()
            if not row:
                raise ValueError('流水不存在')
            if 'category' in changes:
                if changes['category'] not in CATEGORIES:
                    raise ValueError('未知分类')
                con.execute('UPDATE transactions SET category=?,category_manual=1 WHERE id=?', (changes['category'], tx_id))
            if 'note' in changes:
                con.execute('UPDATE transactions SET note=? WHERE id=?', (clean(changes['note'])[:1000], tx_id))
            if changes.get('kind'):
                if changes['kind'] not in KINDS or changes['kind'] == 'duplicate':
                    raise ValueError('重复消费请通过关联功能选择保留记录')
                if row['link_id']:
                    if changes['kind'] != row['kind']:
                        raise ValueError('该流水已经关联，请先解除关联再修改类型')
                else:
                    con.execute('UPDATE transactions SET kind=?,manual=1,review=?,link_reason=? WHERE id=?', (changes['kind'], '' if changes['kind'] != 'unknown' else '收支方向待确认', '用户确认', tx_id))
            self.reconcile(con)

    def link(self, primary_id, secondary_id, kind):
        if kind not in {'transfer', 'duplicate', 'refund'} or primary_id == secondary_id:
            raise ValueError('请选择两条不同记录以及关联类型')
        with self.connection() as con:
            first = con.execute('SELECT * FROM transactions WHERE id=?', (primary_id,)).fetchone()
            second = con.execute('SELECT * FROM transactions WHERE id=?', (secondary_id,)).fetchone()
            if not first or not second:
                raise ValueError('关联记录不存在')
            if first['link_id'] or second['link_id']:
                raise ValueError('记录已有配对，请先解除')
            if kind != 'refund' and first['account_id'] == second['account_id']:
                raise ValueError('账户转移或跨渠道重复应来自不同账单账户，请检查导入账户')
            if kind == 'duplicate' and (first['kind'] != second['kind'] or first['kind'] not in {'expense', 'refund'} or first['cents'] != second['cents']):
                raise ValueError('重复记录必须是两笔等额支出或两笔等额退款；第一条为保留记录')
            if kind == 'refund' and (first['kind'] != 'expense' or second['kind'] != 'refund' or second['cents'] > first['cents']):
                raise ValueError('第一条应为消费，第二条为不超过消费金额的退款')
            if kind == 'transfer' and (first['category'] == '手续费' or second['category'] == '手续费'):
                raise ValueError('手续费应单独计为支出，请不要并入转移')
            kinds = ('transfer', 'transfer') if kind == 'transfer' else (first['kind'], 'duplicate') if kind == 'duplicate' else ('expense', 'refund')
            for record, partner, value in [(first, second, kinds[0]), (second, first, kinds[1])]:
                con.execute("UPDATE transactions SET kind=?,manual=1,review='',link_id=?,link_reason=? WHERE id=?", (value, partner['id'], '用户确认关联；金额不同的转移请另核对手续费', record['id']))
            if kind == 'refund':
                con.execute('UPDATE transactions SET category=?,category_manual=0 WHERE id=?', (first['category'], second['id']))
            self.reconcile(con)

    def unlink(self, tx_id):
        with self.connection() as con:
            row = con.execute('SELECT link_id FROM transactions WHERE id=?', (tx_id,)).fetchone()
            if not row:
                raise ValueError('流水不存在')
            ids = [tx_id] + ([row['link_id']] if row['link_id'] else [])
            for item in ids:
                con.execute("UPDATE transactions SET kind=base_kind,manual=1,link_id=NULL,link_reason='用户解除关联',review='解除关联后需核对收支类型' WHERE id=?", (item,))

    def add_manual(self, value):
        if value['kind'] not in {'income', 'expense', 'refund', 'transfer'} or value['category'] not in CATEGORIES:
            raise ValueError('无效类型或分类')
        amount = cents(value['amount'])
        if amount <= 0:
            raise ValueError('金额请输入大于零的数')
        with self.connection() as con:
            if not con.execute('SELECT 1 FROM accounts WHERE id=?', (value['account_id'],)).fetchone():
                raise ValueError('请选择自己的账单账户')
            con.execute("""INSERT INTO transactions(identity,channel,account_id,time,cents,base_kind,kind,direction,merchant,description,tx_type,status,funding,external_id,merchant_id,category,review,initial_review,note,manual,category_manual,raw,link_reason)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (uuid.uuid4().hex, '手工', value['account_id'], timestamp(value['time']), amount, value['kind'], value['kind'], '', clean(value.get('merchant')), '手工补录', '', '用户补录', '', '', '', value['category'], '', '', clean(value.get('note')), 1, 1, '{}', '用户补录'))

    def rule(self, keyword, category):
        if not clean(keyword) or category not in CATEGORIES:
            raise ValueError('请输入关键词并选择分类')
        with self.connection() as con:
            con.execute('INSERT INTO rules(keyword,category) VALUES(?,?) ON CONFLICT(keyword) DO UPDATE SET category=excluded.category', (clean(keyword), category))
            self.reconcile(con)

    def remove_rule(self, rule_id):
        with self.connection() as con:
            con.execute('DELETE FROM rules WHERE id=?', (rule_id,))
            self.reconcile(con)

    def snapshot(self):
        with self.connection() as con:
            rows = [dict(row) for row in con.execute('SELECT t.*,a.name AS account_name FROM transactions t JOIN accounts a ON a.id=t.account_id ORDER BY time DESC,id DESC')]
            imports = [dict(row, report=json.loads(row['report'])) for row in con.execute('SELECT * FROM imports ORDER BY created DESC')]
            rules = [dict(row) for row in con.execute('SELECT * FROM rules ORDER BY id')]
            accounts = self.accounts(con)
            for row in rows:
                row['raw'] = json.loads(row['raw'])
                row['sources'] = [dict(s) for s in con.execute('SELECT s.import_id,s.row_number,i.name FROM sources s JOIN imports i ON i.id=s.import_id WHERE s.tx_id=?', (row['id'],))]
        return {'transactions': rows, 'accounts': accounts, 'imports': imports, 'rules': rules, 'categories': CATEGORIES, 'kinds': KINDS, 'channels': CHANNELS}

    def backup(self):
        backup_dir = self.root / 'backups'
        backup_dir.mkdir(exist_ok=True)
        target = backup_dir / (datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8] + '.sqlite3')
        with self.connection() as con:
            dest = sqlite3.connect(target)
            try:
                con.backup(dest)
            finally:
                dest.close()
        return target

    def restore(self, blob):
        target = self.root / ('restore-' + uuid.uuid4().hex + '.sqlite3')
        try:
            target.write_bytes(blob)
            con = sqlite3.connect(target)
            try:
                if con.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('数据库校验未通过')
                if con.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0] != '1':
                    raise ValueError('备份版本不兼容')
                with self.connection() as current:
                    for table in ['transactions', 'accounts', 'imports', 'sources', 'rules']:
                        expected = {row[1] for row in current.execute(f'PRAGMA table_info({table})')}
                        actual = {row[1] for row in con.execute(f'PRAGMA table_info({table})')}
                        if not expected <= actual:
                            raise ValueError('备份缺少必要字段')
                if con.execute('SELECT count(*) FROM transactions t LEFT JOIN accounts a ON a.id=t.account_id WHERE a.id IS NULL').fetchone()[0]:
                    raise ValueError('备份存在无账户的流水')
            finally:
                con.close()
            with self.lock:
                previous = self.backup()
                target.replace(self.path)
            return {'previous_backup': previous.name}
        except sqlite3.Error as exc:
            raise ValueError('不是有效的本工具数据库备份') from exc
        finally:
            if target.exists():
                target.unlink()


def summary(rows, month=''):
    chosen = [row for row in rows if not month or row['time'].startswith(month)]
    income = sum(row['cents'] for row in chosen if row['kind'] == 'income')
    spending = sum(row['cents'] for row in chosen if row['kind'] == 'expense')
    refunds = sum(row['cents'] for row in chosen if row['kind'] == 'refund')
    categories = defaultdict(int)
    for row in chosen:
        if row['kind'] in {'expense', 'refund'}:
            categories[row['category']] += row['cents'] * (-1 if row['kind'] == 'refund' else 1)
    return {'income': income, 'spending': spending, 'refunds': refunds, 'net_spending': spending-refunds,
            'balance': income-spending+refunds, 'pending': sum(bool(row['review']) for row in chosen),
            'transfers': sum(row['kind'] == 'transfer' for row in chosen),
            'duplicates': sum(row['kind'] == 'duplicate' for row in chosen), 'categories': dict(categories)}
