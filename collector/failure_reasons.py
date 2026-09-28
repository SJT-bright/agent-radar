"""Translate explicit error metadata; never return raw provider text or IDs."""
import re


def failure_reason(error=None, stop=None):
    data = error if isinstance(error, dict) else {}
    message = error if isinstance(error, str) else data.get('message', '')
    text = message[:8192].lower() if isinstance(message, str) else ''
    status = str(data.get('status', ''))
    if not re.fullmatch(r'[1-5][0-9]{2}', status):
        match = re.search(r'\b(401|403|408|429|500|502|503|504)\b', text)
        status = match.group(1) if match else ''
    suffix = '（HTTP ' + status + '）' if status else ''
    if 'enotfound' in text or '无法解析服务器' in text or 'getaddrinfo' in text:
        return '服务器地址解析失败（DNS）' + suffix
    if status == '429' or 'frequency limit' in text or 'rate limit' in text:
        reason = '使用频率或配额超限' + suffix
        reset = re.search(r'reset at (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC[+-]\d{1,2}(?::\d{2})?)', message if isinstance(message, str) else '', re.I)
        if reset:
            reason += '；日志标注重置时间：' + reset.group(1)
        return reason
    if status in {'401', '403'}:
        return '服务拒绝认证或访问权限' + suffix
    if any(word in text for word in ('context length', 'context window', 'maximum context', '上下文长度')):
        return '上下文超过模型限制' + suffix
    if data.get('isStreamTimeout') is True or any(word in text for word in ('timeout', 'timed out', '超时')) or stop == 'timeout':
        return '请求或响应流超时' + suffix
    if data.get('isNetworkError') is True or any(word in text for word in ('econnreset', 'econnrefused', 'network', 'connection', 'fetch failed', '网络连接')):
        return '网络连接失败或断开' + suffix
    if status.startswith('5'):
        return '服务端返回错误' + suffix
    if stop in {'aborted', 'cancelled', 'canceled', 'stopped', 'interrupted', 'turn_aborted'}:
        return '应用记录了中止事件，未记录触发者或更具体原因'
    if stop == 'killed':
        return '执行被终止，应用未记录触发原因'
    return '应用记录错误终止，未提供可识别的具体原因' + suffix
