"""Local extraction of text PDF statement tables. Never invent transactions."""
import io
import re

MAX_PAGES = 150
MAX_ROWS = 100000


def cell_text(value):
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def header_text(value):
    return re.sub(r'\s+', '', str(value or '')).strip()


def header_index(rows):
    from importer import guess_mapping
    for index, row in enumerate(rows[:30]):
        headers = [header_text(value) for value in row]
        mapping = guess_mapping(headers)
        if 'time' in mapping and ('amount' in mapping or {'income_amount', 'expense_amount'} <= mapping.keys()):
            return index, headers
    return None


def is_continuation(table, headers):
    if not table or not headers or any(len(row) != len(headers) for row in table):
        return False
    from importer import guess_mapping, timestamp
    time_col = headers.index(guess_mapping(headers)['time'])
    rows = [row for row in table if any(cell_text(value) for value in row)]
    if not rows:
        return False
    try:
        timestamp(cell_text(rows[0][time_col]))
    except ValueError:
        return False
    return True


def read_pdf_table(blob, sheet=None, password=None):
    try:
        import pdfplumber
        from pdfminer.pdfdocument import PDFPasswordIncorrect
    except ImportError as exc:
        raise ValueError('PDF 读取依赖未安装，请运行 pip install -r requirements.txt') from exc
    if not blob.startswith(b'%PDF-'):
        raise ValueError('文件不是有效的 PDF')
    groups = {}
    warnings = []
    skipped_pages = []
    text_pages = 0
    total = 0
    previous_headers = None
    try:
        with pdfplumber.open(io.BytesIO(blob), password=password or '') as document:
            if len(document.pages) > MAX_PAGES:
                raise ValueError('PDF 最多支持 150 页，请按月份拆分')
            page_count = len(document.pages)
            for page_number, page in enumerate(document.pages, 1):
                text = page.extract_text() or ''
                if not text.strip():
                    skipped_pages.append(page_number)
                    page.close()
                    continue
                if '(cid:' in text or '\ufffd' in text:
                    raise ValueError(f'PDF 第 {page_number} 页文字编码无法可靠识别，请改用 CSV/XLSX 或可复制文字的 PDF')
                text_pages += 1
                tables = page.extract_tables()
                # Text alignment handles ordinary borderless bank tables.
                if not any(header_index(table) is not None for table in tables):
                    aligned = page.extract_tables({'vertical_strategy': 'text', 'horizontal_strategy': 'text',
                                                   'min_words_vertical': 2, 'text_x_tolerance': 2,
                                                   'text_y_tolerance': 3})
                    if any(header_index(table) is not None or is_continuation(table, previous_headers) for table in aligned):
                        tables = aligned
                found = False
                for table in tables:
                    match = header_index(table)
                    if match:
                        index, headers = match
                        if len(set(headers)) != len(headers) or any(not h for h in headers):
                            warnings.append(f'第 {page_number} 页表头含空列或重复列，未采用此表')
                            continue
                        previous_headers = headers
                        data = list(enumerate(table[index + 1:], index + 2))
                    elif is_continuation(table, previous_headers):
                        # Same columns and a real date are required for a continuation.
                        headers = previous_headers
                        data = [(i + 1, row) for i, row in enumerate(table) if any(cell_text(v) for v in row)]
                    else:
                        continue
                    key = tuple(headers)
                    if key not in groups:
                        groups[key] = {'headers': headers + ['PDF页码', 'PDF表内行号'], 'records': []}
                    for original_row, values in data:
                        if not any(cell_text(value) for value in values):
                            continue
                        if [header_text(value) for value in values] == headers:
                            continue
                        if len(values) != len(headers):
                            raise ValueError(f'PDF 第 {page_number} 页列数不一致，请改用 CSV/XLSX')
                        total += 1
                        if total > MAX_ROWS:
                            raise ValueError('PDF 最多支持十万行，请分批导入')
                        row = dict(zip(headers, [cell_text(value) for value in values]))
                        row.update({'PDF页码': str(page_number), 'PDF表内行号': str(original_row)})
                        groups[key]['records'].append((len(groups[key]['records']) + 2, row))
                    found = True
                if not found:
                    skipped_pages.append(page_number)
                page.close()
    except PDFPasswordIncorrect as exc:
        raise ValueError('PDF 已加密或密码不正确，请输入此 PDF 的打开密码；密码不会保存') from exc
    except ValueError:
        raise
    except Exception as exc:
        wrapped = [exc.__cause__, exc.__context__, *exc.args]
        if any(isinstance(item, PDFPasswordIncorrect) for item in wrapped):
            raise ValueError('PDF 已加密或密码不正确，请输入此 PDF 的打开密码；密码不会保存') from exc
        raise ValueError('PDF 无法读取，请检查文件是否损坏，或改用 CSV/XLSX') from exc
    if not text_pages:
        raise ValueError('这是扫描件或没有可读取文字的 PDF；当前不含 OCR，请导出 CSV/XLSX 或先在本机进行 OCR 后再导入')
    if not groups:
        raise ValueError('未识别出包含日期与金额的交易表格；请改用 CSV/XLSX。当前支持有文字层的普通表格 PDF，不能把月度汇总当作流水导入')
    if skipped_pages:
        warnings.append('以下页面未提取交易表格，请核对是否为封面、扫描页或遗漏明细：' + '、'.join(map(str, skipped_pages)))
    names = [f'表格 {index + 1}' for index in range(len(groups))]
    chosen = sheet or names[0]
    if chosen not in names:
        raise ValueError('PDF 表格不存在，请重新预览')
    result = list(groups.values())[names.index(chosen)]
    from importer import guess_mapping
    return dict(result, mapping=guess_mapping(result['headers']), header_row=1, sheet=chosen, sheets=names,
                format='pdf', pages=page_count, warnings=warnings,
                notice='PDF 为提取结果，请与原件核对笔数、金额及收支列；同结构跨页表格已合并，页码保存在原始字段中。')
