"""Optional, explicit-consent analysis of aggregate ledger numbers only."""
import json
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation
from core import CATEGORIES, summary

PROVIDERS = {
    'openrouter': {
        'name': 'OpenRouter 免费模型', 'base_url': 'https://openrouter.ai/api/v1',
        'default_model': 'openrouter/free', 'docs': 'https://openrouter.ai/openrouter/free',
        'notice': '需填写自己申请的 API Key。仅免费型号，仍受提供方限额与隐私规则约束；汇总数字也属于个人财务信息。',
    },
    'opencode': {
        'name': 'OpenCode Zen（外部调用可能受限）', 'base_url': 'https://opencode.ai/zen/v1',
        'default_model': 'mimo-v2.5-free', 'docs': 'https://opencode.ai/docs/zen/',
        'notice': 'OpenCode 免费层可能仅允许 OpenCode 客户端使用，本工具不绕过限制。部分免费模型可能用输入改进模型；免费规则会变化。',
    },
}
# Official zero-price chat-completions models verified 2026-10-01. No paid fallback.
ZEN_FREE = {'big-pickle', 'mimo-v2.5-free', 'mimo-v2.6-flash-free',
            'ling-3.0-flash-fin-free', 'space-bunny-free', 'longcat-2.5-preview-free'}
MAX_RESPONSE = 2 * 1024 * 1024
SYSTEM_PROMPT = ('你是个人记账分析助手。只根据提供的汇总数据，用简体中文说明收支结构、月度变化、'
                 '可核对的问题及三条具体的日常预算建议。数据金额单位是人民币分。'
                 '本人账户转移、重复记录已经排除；退款按到账月抵减支出。'
                 '有待确认流水时，明确这些统计是暂计；不要推测收入来源、商户、身份或缺失交易。'
                 '收支结余不是账户余额；不提供证券投资推荐。总计以输入为准，不重算或修改账本。'
                 '输出简短清晰的纯文本，最多约800字。')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward a credential or financial payload to a redirect target.


def request_json(url, body=None, api_key='', timeout=45):
    headers = {'Accept': 'application/json', 'User-Agent': 'LocalLedger/1.1'}
    if body is not None:
        headers['Content-Type'] = 'application/json'
    if api_key:
        if len(api_key) > 512 or any(c in api_key for c in '\r\n'):
            raise ValueError('API Key 格式不正确')
        headers['Authorization'] = 'Bearer ' + api_key
    req = urllib.request.Request(url, headers=headers,
                                 data=json.dumps(body, ensure_ascii=False).encode('utf-8') if body is not None else None)
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            raise ValueError('模型响应过大，请更换模型')
        return json.loads(raw)
    except urllib.error.HTTPError as exc:
        # Upstream error bodies may echo secrets/prompts. Never return or log them.
        messages = {401: 'API Key 无效或未获授权，请重新申请或填写。',
                    402: '提供方要求额度或付款，本工具不会充值，也不会切换付费模型。',
                    403: '提供方拒绝调用；OpenCode 免费层可能限制外部客户端。可改选 OpenRouter。',
                    404: '模型或接口已下线，请刷新免费模型列表。',
                    429: '免费额度或请求频率已达上限，请稍后重试。'}
        raise ValueError(messages.get(exc.code, f'模型服务返回 HTTP {exc.code}，请稍后重试或更换免费模型。')) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ValueError('无法连接模型服务或请求超时，请检查网络后重试；账本没有改动。') from None
    except (json.JSONDecodeError, UnicodeError):
        raise ValueError('模型服务返回了无法识别的响应') from None


def provider_spec(provider):
    if provider not in PROVIDERS:
        raise ValueError('未知模型接口')
    return PROVIDERS[provider]


def free_model(provider, model):
    provider_spec(provider)
    if not isinstance(model, str) or len(model) > 160 or not re.fullmatch(r'[a-zA-Z0-9._:/-]+', model):
        raise ValueError('模型编号不正确')
    if provider == 'openrouter':
        if model != 'openrouter/free' and not model.endswith(':free'):
            raise ValueError('仅允许 OpenRouter 免费路由或 :free 型号，不会调用付费模型')
    elif model not in ZEN_FREE:
        raise ValueError('该 OpenCode 型号不在已核验的免费聊天模型清单中')
    return model


def free_catalog(provider):
    spec = provider_spec(provider)
    result = request_json(spec['base_url'] + '/models', timeout=12)
    models = []
    for item in result.get('data', []):
        model = item.get('id', '')
        try:
            free_model(provider, model)
        except ValueError:
            continue
        if provider == 'openrouter':
            pricing = item.get('pricing', {})
            try:
                if any(Decimal(str(pricing.get(key, '-1'))) != 0 for key in ('prompt', 'completion')):
                    continue
            except InvalidOperation:
                continue
        models.append(model)
    return {'models': sorted(set(models)), 'notice': spec['notice']}


def aggregate_payload(snapshot, month=''):
    if month and not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', month):
        raise ValueError('请选择有效的统计月份')
    # Construct a whitelist payload; never forward raw rows or user-authored names/notes.
    rows = snapshot['transactions']
    selected = [row for row in rows if not month or row['time'].startswith(month)]
    stats = summary(selected)
    category_totals = {category: 0 for category in CATEGORIES}
    for row in selected:
        if row['kind'] in {'expense', 'refund'}:
            category = row['category'] if row['category'] in CATEGORIES else '其他'
            category_totals[category] += row['cents'] * (-1 if row['kind'] == 'refund' else 1)
    months = sorted({row['time'][:7] for row in rows if re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', row['time'][:7])})
    # Restrict trends to the approved month when one is selected.
    trend_months = [month] if month else months[-12:]
    trend = []
    for period in trend_months:
        item = summary(rows, period)
        trend.append({'month': period, 'income': item['income'], 'net_spending': item['net_spending'],
                      'pending': item['pending']})
    return {'currency': 'CNY', 'amount_unit': '分', 'period': month or '全部月份',
            'record_count': len(selected),
            'totals': {key: stats[key] for key in ('income', 'spending', 'refunds', 'net_spending', 'balance', 'pending', 'transfers', 'duplicates')},
            'net_spending_by_category': {key: value for key, value in category_totals.items() if value},
            'monthly_totals': trend,
            'rules': ['本人账户转移和重复流水已排除收支', '手续费作为支出', '退款按到账月抵减支出',
                      '收支结余不代表账户余额', '待确认笔数非零时统计为暂计']}


def local_report(payload):
    t = payload['totals']
    fmt = lambda value: f'¥{value / 100:,.2f}'
    lines = [f"{payload['period']} · 本地分析（未调用模型、未联网）",
             f"收入 {fmt(t['income'])}；消费 {fmt(t['spending'])}；退款 {fmt(t['refunds'])}；净支出 {fmt(t['net_spending'])}。",
             f"收支结余 {fmt(t['balance'])}，不代表账户余额。已排除 {t['transfers']} 条账户转移和 {t['duplicates']} 条重复记录。"]
    categories = sorted(payload['net_spending_by_category'].items(), key=lambda item: item[1], reverse=True)
    if categories:
        lines.append('主要净支出分类：' + '；'.join(f'{name} {fmt(amount)}' for name, amount in categories[:3]) + '。')
    if t['pending']:
        lines.append(f"有 {t['pending']} 条待确认流水，请先核对，当前为暂计统计。")
    elif not payload['record_count']:
        lines.append('此范围暂无流水，请先导入账单。')
    else:
        lines.append('建议先核对本人转移与重复扣款，再为主要支出分类设定下月预算。')
    return '\n\n'.join(lines)


class AnalysisService:
    def __init__(self):
        self.lock = threading.Lock()
        self.previews = {}
        self.busy = set()

    def prepare(self, snapshot, scope, provider, model, month=''):
        spec = provider_spec(provider)
        free_model(provider, model)
        payload = aggregate_payload(snapshot, month)
        consent = secrets.token_urlsafe(32)
        prepared = {'scope': scope, 'provider': provider, 'model': model, 'month': month,
                    'payload': payload, 'expires': time.monotonic() + 600}
        with self.lock:
            self.previews = {key: val for key, val in self.previews.items() if val['expires'] > time.monotonic()}
            if len(self.previews) > 100:
                raise ValueError('预览请求过多，请稍后重试')
            self.previews[consent] = prepared
        return {'consent_token': consent, 'provider': provider, 'provider_name': spec['name'],
                'endpoint': spec['base_url'] + '/chat/completions', 'model': model, 'payload': payload,
                'system_prompt': SYSTEM_PROMPT, 'notice': spec['notice'], 'expires_in_seconds': 600}

    def run(self, snapshot, scope, consent_token, confirmed, api_key):
        if confirmed is not True:
            raise ValueError('请先预览并确认将汇总数据发送到所选模型服务')
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError('请填写自己的 API Key；密钥只用于本次请求，不会保存')
        if not isinstance(consent_token, str):
            raise ValueError('请重新预览本次发送的数据')
        with self.lock:
            preview = self.previews.get(consent_token)
            if not preview or preview['scope'] != scope or preview['expires'] <= time.monotonic():
                raise ValueError('发送预览已失效，请重新预览并确认')
            if scope in self.busy:
                raise ValueError('已有分析正在进行，请等待完成')
            if aggregate_payload(snapshot, preview['month']) != preview['payload']:
                raise ValueError('账本已发生变化，请重新预览后再发送')
            self.previews.pop(consent_token)
            self.busy.add(scope)
        try:
            provider, model = preview['provider'], preview['model']
            spec = provider_spec(provider)
            free_model(provider, model)
            if provider == 'openrouter':
                # Refresh current price metadata before sending financial aggregates.
                if model not in free_catalog(provider)['models']:
                    raise ValueError('型号未出现在当前零价清单中，请刷新模型列表并重新选择')
            body = {'model': model, 'messages': [
                {'role': 'system', 'content': SYSTEM_PROMPT},
                {'role': 'user', 'content': json.dumps(preview['payload'], ensure_ascii=False)}],
                'temperature': 0.2, 'max_tokens': 1600, 'stream': False}
            if provider == 'openrouter':
                body['provider'] = {'max_price': {'prompt': 0, 'completion': 0, 'request': 0},
                                    'allow_fallbacks': False}
            result = request_json(spec['base_url'] + '/chat/completions', body, api_key.strip())
            try:
                content = result['choices'][0]['message']['content']
            except (KeyError, IndexError, TypeError):
                raise ValueError('模型未返回可读取的分析结果，请更换免费模型') from None
            if not isinstance(content, str) or not content.strip():
                raise ValueError('模型返回了空内容，请更换免费模型')
            return {'analysis': content[:20000], 'model': model,
                    'provider': provider, 'actual_model': str(result.get('model', model))[:160],
                    'notice': '模型建议仅供核对与预算参考，未修改任何账本记录。'}
        finally:
            with self.lock:
                self.busy.discard(scope)
