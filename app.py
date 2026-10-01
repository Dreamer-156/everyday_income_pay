"""Loopback-only UI, local imports and opt-in aggregate model analysis."""
import argparse
import hashlib
import os
import sys
import logging
import base64
import csv
import io
import json
import secrets
import sqlite3
import socket
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from core import Ledger, KINDS
from importer import read_table
from analysis import AnalysisService, PROVIDERS, free_catalog, aggregate_payload, local_report

ROOT = Path(__file__).resolve().parent
MAX_BODY = 30 * 1024 * 1024
INSTANCE_ID = hashlib.sha256(str(ROOT).casefold().encode('utf-8')).hexdigest()[:16]


def safe_cell(value):
    text = str(value if value is not None else '')
    return "'" + text if text.lstrip().startswith(('=', '+', '-', '@')) else text


def export_rows(rows, month='', xlsx=False):
    selected = [r for r in rows if not month or r['time'].startswith(month)]
    headers = ['记录编号', '交易时间', '渠道', '账单账户', '金额(元)', '统计类型', '分类', '交易对方', '说明', '支付方式', '待确认原因', '对应记录', '关联说明', '备注', '来源文件与行']
    values = [[r['id'], r['time'], r['channel'], r['account_name'], f"{r['cents']/100:.2f}", KINDS[r['kind']], r['category'], r['merchant'], r['description'], r['funding'], r['review'], r['link_id'] or '', r['link_reason'], r['note'], '; '.join(f"{s['name']}:{s['row_number']}" for s in r['sources'])] for r in selected]
    if xlsx:
        from openpyxl import Workbook
        from openpyxl.styles import Font
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = '收支流水'
        sheet.append(headers)
        for row in values:
            sheet.append([safe_cell(v) for v in row])
        sheet.freeze_panes = 'A2'
        sheet.auto_filter.ref = sheet.dimensions
        for cell in sheet[1]:
            cell.font = Font(bold=True)
        for column in 'ABCDEFGHIJKLMNO':
            sheet.column_dimensions[column].width = 22
        output = io.BytesIO()
        workbook.save(output)
        return output.getvalue()
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    writer.writerow(headers)
    writer.writerows([[safe_cell(v) for v in row] for row in values])
    return output.getvalue().encode('utf-8-sig')


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != 'nt'

    def server_bind(self):
        # Windows SO_REUSEADDR can allow two services to bind the same address.
        # Exclusive binding is necessary for a dependable fixed local endpoint.
        if os.name == 'nt':
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def __init__(self, address, data_root):
        super().__init__(address, Handler)
        self.token = secrets.token_urlsafe(32)
        self.data_root = Path(data_root)
        self.ledger = Ledger(self.data_root / 'user')
        self.demo_ledger = None
        self.demo_lock = threading.Lock()
        self.analysis_service = AnalysisService()

    def demo(self):
        with self.demo_lock:
            if self.demo_ledger is None:
                ledger = Ledger(self.data_root / 'demo')
                if not ledger.snapshot()['transactions']:
                    ledger.account('招商银行(1234)', ['招商银行（1234）', '招行尾号1234'])
                    ledger.rule('午餐', '餐饮')
                    ledger.rule('教材', '学习')
                    ledger.rule('生活费', '生活费')
                    ledger.import_file('微信示例.csv', (ROOT / 'examples' / 'wechat.csv').read_bytes(), '微信', 1)
                    ledger.import_file('支付宝示例.csv', (ROOT / 'examples' / 'alipay.csv').read_bytes(), '支付宝', 2)
                    ledger.import_file('银行卡示例.csv', (ROOT / 'examples' / 'bank.csv').read_bytes(), '银行卡', 3)
                    rows = ledger.snapshot()['transactions']
                    purchase = next(r for r in rows if r['external_id'] == 'wx-book')
                    refund = next(r for r in rows if r['external_id'] == 'wx-refund')
                    ledger.link(purchase['id'], refund['id'], 'refund')
                self.demo_ledger = ledger
            return self.demo_ledger


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Do not write file names, merchants, query contents or body data to logs.
        pass

    def send(self, code, body, content_type='application/json; charset=utf-8', filename=None):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if filename:
            self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    def allowed(self, api=False):
        if self.headers.get('Host') not in {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}:
            self.send(403, {'error': '仅允许本机访问'})
            return False
        origin = self.headers.get('Origin')
        if origin and origin not in {f'http://127.0.0.1:{self.server.server_port}', f'http://localhost:{self.server.server_port}'}:
            self.send(403, {'error': '拒绝跨站请求'})
            return False
        if api and not secrets.compare_digest(self.headers.get('X-Ledger-Token', ''), self.server.token):
            self.send(403, {'error': '请刷新本机工具页面'})
            return False
        return True

    def route(self):
        parsed = urlparse(self.path)
        demo = parsed.path.startswith('/demo/api/')
        return parsed, parsed.path.replace('/demo/api/', '/api/', 1) if demo else parsed.path, self.server.demo() if demo else self.server.ledger

    def do_GET(self):
        try:
            if not self.allowed():
                return
            parsed = urlparse(self.path)
            if parsed.path == '/health':
                # Liveness only: no financial data, file paths or API token.
                self.send(200, {'app': 'local-ledger', 'instance': INSTANCE_ID, 'port': self.server.server_port, 'pid': os.getpid()})
                return
            if parsed.path == '/analysis-ui.js':
                self.send(200, (ROOT / 'analysis-ui.js').read_bytes(), 'text/javascript; charset=utf-8')
                return
            if parsed.path == '/':
                page = (ROOT / 'ui.html').read_text(encoding='utf-8-sig').replace('__TOKEN__', self.server.token)
                self.send(200, page.encode('utf-8'), 'text/html; charset=utf-8')
                return
            if not self.allowed(api=True):
                return
            parsed, route, ledger = self.route()
            query = parse_qs(parsed.query)
            if route == '/api/analysis/providers':
                self.send(200, {'providers': [dict(id=key, **spec) for key, spec in PROVIDERS.items()]})
            elif route == '/api/state':
                self.send(200, ledger.snapshot())
            elif route in {'/api/export.csv', '/api/export.xlsx'}:
                xlsx = route.endswith('xlsx')
                blob = export_rows(ledger.snapshot()['transactions'], query.get('month', [''])[0], xlsx)
                self.send(200, blob, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' if xlsx else 'text/csv; charset=utf-8', 'ledger.xlsx' if xlsx else 'ledger.csv')
            elif route == '/api/backup':
                target = ledger.backup()
                self.send(200, target.read_bytes(), 'application/octet-stream', target.name)
            else:
                self.send(404, {'error': '页面不存在'})
        except Exception as exc:
            self.send(400, {'error': self.friendly(exc)})

    @staticmethod
    def friendly(exc):
        if isinstance(exc, (ValueError, KeyError, TypeError, sqlite3.Error)):
            return str(exc)
        return '文件无法读取或操作失败，请检查格式；支持 CSV、TSV、XLSX 和文字表格 PDF'

    def do_POST(self):
        try:
            if not self.allowed(api=True):
                return
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= MAX_BODY:
                raise ValueError('文件或请求过大，单文件最多 20 MB')
            value = json.loads(self.rfile.read(length))
            parsed, route, ledger = self.route()
            if route in {'/api/preview', '/api/import'}:
                blob = base64.b64decode(value['content'], validate=True)
                if len(blob) > 20*1024*1024:
                    raise ValueError('单文件最多 20 MB，请分批导入')
                if route == '/api/preview':
                    table = read_table(value['name'], blob, value.get('header_row'), value.get('sheet'), value.get('password'))
                    result = {k: v for k, v in table.items() if k != 'records'}
                    result.update(sample=table['records'][:5], count=len(table['records']))
                else:
                    result = ledger.import_file(value['name'], blob, value['channel'], int(value['account_id']), value.get('mapping'), value.get('header_row'), value.get('sheet'), value.get('password'))
            elif route == '/api/analysis/models':
                result = free_catalog(value['provider'])
            elif route == '/api/analysis/local':
                payload = aggregate_payload(ledger.snapshot(), value.get('month', ''))
                result = {'analysis': local_report(payload), 'payload': payload}
            elif route == '/api/analysis/preview':
                result = self.server.analysis_service.prepare(ledger.snapshot(), str(ledger.root),
                    value['provider'], value['model'], value.get('month', ''))
            elif route == '/api/analysis/run':
                result = self.server.analysis_service.run(ledger.snapshot(), str(ledger.root),
                    value.get('consent_token'), value.get('confirmed'), value.get('api_key', ''))
            elif route == '/api/account':
                ledger.account(value['name'], value.get('aliases', []))
                result = {'ok': True}
            elif route == '/api/edit':
                ledger.edit(int(value['id']), value['changes'])
                result = {'ok': True}
            elif route == '/api/link':
                ledger.link(int(value['primary_id']), int(value['secondary_id']), value['kind'])
                result = {'ok': True}
            elif route == '/api/unlink':
                ledger.unlink(int(value['id']))
                result = {'ok': True}
            elif route == '/api/manual':
                ledger.add_manual(value)
                result = {'ok': True}
            elif route == '/api/rule':
                ledger.rule(value['keyword'], value['category'])
                result = {'ok': True}
            elif route == '/api/remove-rule':
                ledger.remove_rule(int(value['id']))
                result = {'ok': True}
            elif route == '/api/restore':
                result = ledger.restore(base64.b64decode(value['content'], validate=True))
            else:
                self.send(404, {'error': '接口不存在'})
                return
            self.send(200, result)
        except Exception as exc:
            self.send(400, {'error': self.friendly(exc)})


def main():
    parser = argparse.ArgumentParser(description='本机个人收支工具')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--data-dir', type=Path, default=ROOT / 'data')
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--strict-port', action='store_true', help='后台服务保持固定地址，不自动切换端口')
    args = parser.parse_args()
    log_dir = args.data_dir / 'runtime'
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=log_dir / 'service.log', encoding='utf-8',
                        level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    server = None
    for port in range(args.port, args.port+(1 if args.strict_port else 10)):
        try:
            server = Server(('127.0.0.1', port), args.data_dir)
            break
        except OSError:
            continue
    if server is None:
        raise SystemExit('端口不可用，请用 --port 指定其他端口')
    url = f'http://127.0.0.1:{server.server_port}'
    logging.info('Service started at %s', url)
    if sys.stdout is not None:
        print(f'收支工具已启动：{url}\n数据目录：{args.data_dir}\n关闭此窗口或按 Ctrl+C 可停止。', flush=True)
    if not args.no_browser:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logging.info('Service stopped by interrupt')
    except Exception:
        logging.exception('Service failed')
        raise
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
