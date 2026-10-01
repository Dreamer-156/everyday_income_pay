import base64
import io
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import Server
from core import Ledger, summary
from importer import read_table
from analysis import AnalysisService, aggregate_payload, free_catalog, free_model, local_report, request_json


def pdf_bill(pages=None, lines=True, blank=False, repeat_header=True):
    """Synthetic statements only. Production imports do not depend on reportlab."""
    from reportlab.pdfgen import canvas
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))
    output = io.BytesIO()
    doc = canvas.Canvas(output, pagesize=(760, 500))
    headers = ['交易日期', '收入金额', '支出金额', '摘要', '流水号']
    pages = pages or [[['2026-10-01', '100.00', '-', '工资', 'pdf-income'],
                       ['2026-10-02', '-', '12.00', '午餐', 'pdf-meal']],
                      [['2026-10-03', '-', '24.50', '教材', 'pdf-book']]]
    for page_index, rows in enumerate(pages):
        doc.setFont('STSong-Light', 10)
        if not blank:
            doc.drawString(25, 465, '模拟人民币账单（非真实账单）')
            xs = [25, 150, 245, 340, 500, 730]
            drawn_rows = ([headers] if page_index == 0 or repeat_header else []) + rows
            for row_index, row in enumerate(drawn_rows):
                y = 420 - row_index * 30
                for col, value in enumerate(row):
                    doc.drawString(xs[col] + 5, y, value)
            if lines:
                top, bottom = 440, 405 - len(rows) * 30
                for x in xs:
                    doc.line(x, top, x, bottom)
                for row_index in range(len(rows) + 2):
                    doc.line(xs[0], top - row_index * 30, xs[-1], top - row_index * 30)
        doc.showPage()
    doc.save()
    return output.getvalue()


class PdfTests(unittest.TestCase):
    def test_multipage_chinese_tables_and_original_provenance(self):
        blob = pdf_bill()
        table = read_table('bank.pdf', blob)
        self.assertEqual(table['format'], 'pdf')
        self.assertEqual(table['pages'], 2)
        self.assertEqual(len(table['records']), 3)
        self.assertEqual(table['records'][2][1]['PDF页码'], '2')
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(directory)
            report = ledger.import_file('bank.pdf', blob, '银行卡', 1)
            self.assertEqual((report['new'], report['errors']), (3, []))
            stats = summary(ledger.snapshot()['transactions'])
            self.assertEqual((stats['income'], stats['net_spending']), (10000, 3650))
            self.assertTrue(ledger.import_file('bank.pdf', blob, '银行卡', 1)['repeated_file'])
            self.assertEqual(next(ledger.raw_dir.glob('*.pdf')).read_bytes(), blob)

    def test_borderless_text_table(self):
        table = read_table('bank.pdf', pdf_bill(lines=False))
        self.assertEqual(len(table['records']), 3)
        self.assertEqual(table['records'][0][1]['收入金额'], '100.00')

    def test_borderless_continuation_without_repeated_header(self):
        pages = [[['2026-10-01', '100.00', '-', '工资', 'pdf-income'],
                  ['2026-10-02', '-', '12.00', '午餐', 'pdf-meal']],
                 [['2026-10-03', '-', '24.50', '教材', 'pdf-book'],
                  ['2026-10-04', '-', '5.00', '午餐', 'pdf-meal2']]]
        table = read_table('bank.pdf', pdf_bill(pages, lines=False, repeat_header=False))
        self.assertEqual(len(table['records']), 4)
        self.assertEqual(table['records'][3][1]['PDF页码'], '2')
        self.assertEqual(table['records'][2][1]['支出金额'], '24.50')
        self.assertEqual(table['warnings'], [])

    def test_encrypted_pdf_password_never_stored(self):
        from pypdf import PdfReader, PdfWriter
        writer = PdfWriter()
        writer.append(PdfReader(io.BytesIO(pdf_bill())))
        writer.encrypt('fixture-password')
        output = io.BytesIO(); writer.write(output); blob = output.getvalue()
        for password in (None, 'wrong'):
            with self.assertRaisesRegex(ValueError, '密码'):
                read_table('bank.pdf', blob, password=password)
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(directory)
            report = ledger.import_file('bank.pdf', blob, '银行卡', 1, password='fixture-password')
            self.assertEqual(report['new'], 3)
            self.assertNotIn('fixture-password', json.dumps(ledger.snapshot(), ensure_ascii=False))

    def test_empty_scan_and_invalid_pdf_are_not_imported(self):
        with self.assertRaisesRegex(ValueError, '扫描件'):
            read_table('scan.pdf', pdf_bill(blank=True))
        with self.assertRaisesRegex(ValueError, '有效的 PDF'):
            read_table('wrong.pdf', b'not a pdf')

    def test_invalid_money_row_reported_with_page_trace(self):
        blob = pdf_bill([[['2026-10-01', '-', 'bad', '午餐', 'pdf-error'],
                          ['2026-10-02', '-', '12.00', '午餐', 'pdf-valid']]])
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(directory)
            report = ledger.import_file('bank.pdf', blob, '银行卡', 1)
            self.assertEqual((report['new'], len(report['errors'])), (1, 1))
            row = ledger.snapshot()['transactions'][0]
            self.assertEqual(row['raw']['PDF页码'], '1')

    def test_non_table_page_warning_is_visible(self):
        from pypdf import PdfReader, PdfWriter
        writer = PdfWriter(); writer.append(PdfReader(io.BytesIO(pdf_bill())))
        writer.add_blank_page(width=760, height=500)
        output = io.BytesIO(); writer.write(output)
        table = read_table('mixed.pdf', output.getvalue())
        self.assertIn('3', ' '.join(table['warnings']))


def snapshot():
    return {'transactions': [
        {'time': '2026-10-01 00:00:00', 'cents': 10000, 'kind': 'income', 'category': '其他', 'review': '',
         'merchant': 'private-name', 'account_name': 'card-621234567890', 'description': 'private-merchant', 'note': 'private-note'},
        {'time': '2026-10-02 00:00:00', 'cents': 1200, 'kind': 'expense', 'category': '餐饮', 'review': ''},
        {'time': '2026-10-03 00:00:00', 'cents': 200, 'kind': 'refund', 'category': '餐饮', 'review': ''},
        {'time': '2026-10-04 00:00:00', 'cents': 5000, 'kind': 'transfer', 'category': '其他', 'review': ''},
        {'time': '2026-09-01 00:00:00', 'cents': 700, 'kind': 'expense', 'category': 'private-category', 'review': 'private-review'}]}


class AnalysisTests(unittest.TestCase):
    def test_only_whitelisted_aggregates_and_selected_month_sent(self):
        payload = aggregate_payload(snapshot(), '2026-10')
        text = json.dumps(payload, ensure_ascii=False)
        for value in ('private-name', '621234', 'private-merchant', 'private-note', 'private-review', '2026-09', 'private-category'):
            self.assertNotIn(value, text)
        self.assertEqual((payload['totals']['income'], payload['totals']['net_spending']), (10000, 1000))
        self.assertEqual(payload['totals']['transfers'], 1)
        all_payload = aggregate_payload(snapshot())
        self.assertIn('其他', all_payload['net_spending_by_category'])
        self.assertNotIn('private-category', json.dumps(all_payload))

    def test_local_report_uses_same_accounting_without_network(self):
        with patch('analysis.request_json') as network:
            text = local_report(aggregate_payload(snapshot(), '2026-10'))
            self.assertIn('¥90.00', text)
            self.assertIn('不代表账户余额', text)
            network.assert_not_called()

    def test_paid_models_rejected(self):
        for provider, model in [('openrouter', 'openai/gpt-4o'), ('opencode', 'minimax-m2.5'), ('opencode', 'nemotron-3-ultra-free')]:
            with self.assertRaises(ValueError):
                free_model(provider, model)

    def test_current_catalog_price_filter(self):
        data = {'data': [{'id': 'openrouter/free', 'pricing': {'prompt': '0', 'completion': '0'}},
                         {'id': 'test/a:free', 'pricing': {'prompt': '0', 'completion': '0.1'}},
                         {'id': 'test/b:free', 'pricing': {'prompt': '0', 'completion': '0'}},
                         {'id': 'test/paid', 'pricing': {'prompt': '0', 'completion': '0'}}]}
        with patch('analysis.request_json', return_value=data):
            self.assertEqual(free_catalog('openrouter')['models'], ['openrouter/free', 'test/b:free'])

    def test_preview_confirmation_expiry_and_cross_ledger_rejected(self):
        service = AnalysisService()
        preview = service.prepare(snapshot(), 'user', 'openrouter', 'openrouter/free', '2026-10')
        with patch('analysis.request_json') as network:
            for scope, confirmed in [('user', False), ('demo', True)]:
                with self.assertRaises(ValueError):
                    service.run(snapshot(), scope, preview['consent_token'], confirmed, 'fixture-key')
            service.previews[preview['consent_token']]['expires'] = 0
            with self.assertRaisesRegex(ValueError, '失效'):
                service.run(snapshot(), 'user', preview['consent_token'], True, 'fixture-key')
            network.assert_not_called()

    def test_changed_ledger_requires_new_confirmation(self):
        service = AnalysisService()
        preview = service.prepare(snapshot(), 'user', 'openrouter', 'openrouter/free', '2026-10')
        changed = snapshot(); changed['transactions'][0]['cents'] += 1
        with patch('analysis.request_json') as network:
            with self.assertRaisesRegex(ValueError, '发生变化'):
                service.run(changed, 'user', preview['consent_token'], True, 'fixture-key')
            network.assert_not_called()

    def test_request_is_price_limited_single_use_and_key_not_in_result(self):
        service = AnalysisService()
        preview = service.prepare(snapshot(), 'user', 'openrouter', 'openrouter/free', '2026-10')
        catalog = {'data': [{'id': 'openrouter/free', 'pricing': {'prompt': '0', 'completion': '0'}}]}
        with patch('analysis.request_json', side_effect=[catalog, {'choices': [{'message': {'content': 'fixture analysis'}}]}]) as network:
            result = service.run(snapshot(), 'user', preview['consent_token'], True, 'fixture-key')
            self.assertEqual(result['analysis'], 'fixture analysis')
            body = network.call_args.args[1]
            self.assertEqual(body['provider']['max_price'], {'prompt': 0, 'completion': 0, 'request': 0})
            self.assertNotIn('fixture-key', json.dumps(result))
            self.assertNotIn('private-name', json.dumps(body))
            with self.assertRaisesRegex(ValueError, '失效'):
                service.run(snapshot(), 'user', preview['consent_token'], True, 'fixture-key')
            self.assertEqual(network.call_count, 2)

    def test_upstream_error_does_not_expose_key_or_prompt(self):
        error = urllib.error.HTTPError('https://example.invalid', 429, 'fixture-key private-name', {}, io.BytesIO(b'private-response'))
        with patch('analysis.urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaisesRegex(ValueError, '上限') as caught:
                request_json('https://example.invalid', {}, 'fixture-key')
        for value in ('fixture-key', 'private-name', 'private-response'):
            self.assertNotIn(value, str(caught.exception))


class FeatureHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = Server(('127.0.0.1', 0), self.tmp.name)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(); self.tmp.cleanup()

    def post(self, route, body, token=True):
        headers = {'Content-Type': 'application/json'}
        if token: headers['X-Ledger-Token'] = self.server.token
        request = urllib.request.Request(self.url + route, data=json.dumps(body).encode(), headers=headers)
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)

    def test_pdf_preview_then_import_through_http(self):
        params = {'name': 'bank.pdf', 'content': base64.b64encode(pdf_bill()).decode()}
        preview = self.post('/api/preview', params)
        self.assertEqual((preview['count'], preview['format']), (3, 'pdf'))
        params.update(mapping=preview['mapping'], sheet=preview['sheet'], header_row=preview['header_row'], channel='银行卡', account_id=1)
        result = self.post('/api/import', params)
        self.assertEqual((result['new'], result['errors']), (3, []))

    def test_analysis_requires_token_and_confirmed_preview(self):
        with self.assertRaises(urllib.error.HTTPError):
            self.post('/api/analysis/local', {'month': ''}, token=False)
        local = self.post('/api/analysis/local', {'month': ''})
        self.assertIn('暂无流水', local['analysis'])
        preview = self.post('/api/analysis/preview', {'month': '', 'provider': 'openrouter', 'model': 'openrouter/free'})
        with patch('analysis.request_json') as network:
            with self.assertRaises(urllib.error.HTTPError):
                self.post('/api/analysis/run', {'consent_token': preview['consent_token'], 'confirmed': False, 'api_key': 'fixture-key'})
            network.assert_not_called()
        self.assertEqual(self.server.ledger.snapshot()['transactions'], [])


if __name__ == '__main__':
    unittest.main()
