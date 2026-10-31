# -*- coding: utf-8 -*-
"""
语义改名（大模型）——可选增强层

设计约束（与规则引擎严格隔离）：
1. 不配置就完全不参与，规则引擎照常工作
2. 走 OpenAI 兼容协议：DeepSeek / 通义 / Kimi / 硅基流动 / Ollama 通吃
3. 零依赖：urllib 直发 HTTP，不需要 openai SDK
4. 模型只管"提出新名字"，合法性、冲突、写入全部由后端复核
5. 主动跳过含扩展名的部分，模型碰不到扩展名

配置文件：data/llm.json
"""

import json
import os
import re
import urllib.error
import urllib.request
import uuid
import zipfile

from . import engine

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, 'data')
CONF = os.path.join(DATA, 'llm.json')

DEFAULT_PROMPT = (
    '你是中文文件命名助手。下面是若干文件的原始名称，请把它们改写成通顺、规范、'
    '符合中文公文与办公习惯的标题。\n'
    '要求：\n'
    '1. 保持原名中的关键信息（单位、事项、编号、日期）不丢失\n'
    '2. 去掉「副本」「新建」「最终版」「(1)」这类无意义残留\n'
    '3. 日期统一写成 2026-01-05 的格式，并放在最前面\n'
    '4. 不要输出扩展名，不要输出任何解释\n'
    '5. 严格按输入顺序，输出 JSON 数组，元素为字符串'
)

DEFAULT_CONF = {
    'enabled': False,
    'base_url': 'http://127.0.0.1:11434/v1',
    'api_key': '',
    'model': 'qwen2.5:7b',
    'timeout': 120,
    'batch_size': 30,
    'temperature': 0.2,
    'prompt': DEFAULT_PROMPT,
}


def load_conf():
    cfg = dict(DEFAULT_CONF)
    if os.path.exists(CONF):
        try:
            with open(CONF, 'r', encoding='utf-8') as f:
                cfg.update(json.load(f) or {})
        except Exception:
            pass
    return cfg


def save_conf(cfg):
    """
    从现有配置合并，不能从 DEFAULT_CONF 重建。
    否则前端留空 api_key（表示“不修改”）时，会把已保存的 Key 清空。
    """
    os.makedirs(DATA, exist_ok=True)
    merged = load_conf()
    for k, v in (cfg or {}).items():
        if k not in DEFAULT_CONF:
            continue
        if k == 'api_key':
            if v is None:
                merged[k] = ''         # 显式 null 才表示清空
            elif isinstance(v, str):
                merged[k] = v
            else:
                raise ValueError('api_key 必须是字符串或 null')
        elif k == 'enabled':
            merged[k] = v is True or str(v).lower() == 'true'
        elif k in ('timeout', 'batch_size', 'temperature'):
            try:
                merged[k] = float(v) if k == 'temperature' else int(v)
            except (TypeError, ValueError):
                continue
        else:
            merged[k] = v
    merged['timeout'] = max(5, min(600, int(merged.get('timeout', 120))))
    merged['batch_size'] = max(1, min(100, int(merged.get('batch_size', 30))))
    merged['temperature'] = max(0.0, min(2.0, float(merged.get('temperature', 0.2))))
    tmp = CONF + '.tmp.' + uuid.uuid4().hex
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONF)
    return merged


def is_ready(cfg=None):
    cfg = cfg or load_conf()
    return bool(cfg.get('enabled') and cfg.get('base_url') and cfg.get('model'))


# ============================================================
# 文件内容抽取（给"按内容起标题"用）
# ============================================================

# 只解压前 2 MiB：足够取 600 字，避免压缩炸弹/超大 XML 把内存打爆。
_MAX_XML_BYTES = 2 * 1024 * 1024


def _zip_text(z, name):
    with z.open(name) as f:
        return f.read(_MAX_XML_BYTES).decode('utf-8', 'ignore')


def read_snippet(path, max_chars=600):
    """
    取文件开头一段文本。纯标准库，不启动任何 Office 程序。
    .docx 走 zip + XML 解析（不会锁文件、不碰 Word）。
    读不了的类型返回空串，不影响流程。
    """
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext in ('.txt', '.md', '.csv', '.log', '.json', '.xml', '.html', '.htm'):
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                return f.read(max_chars).strip()

        if ext == '.docx':
            with zipfile.ZipFile(path) as z:
                xml = _zip_text(z, 'word/document.xml')
            xml = re.sub(r'</w:p>', '\n', xml)
            text = re.sub(r'<[^>]+>', '', xml)
            text = re.sub(r'\n{2,}', '\n', text)
            return text.strip()[:max_chars]

        if ext == '.pptx':
            parts = []
            with zipfile.ZipFile(path) as z:
                names = sorted(n for n in z.namelist()
                               if re.match(r'ppt/slides/slide\d+\.xml$', n))
                for n in names[:8]:
                    parts.append(re.sub(r'<[^>]+>', ' ', _zip_text(z, n)))
            text = re.sub(r'\s{2,}', ' ', ' '.join(parts))
            return text.strip()[:max_chars]

        if ext == '.xlsx':
            with zipfile.ZipFile(path) as z:
                if 'xl/sharedStrings.xml' in z.namelist():
                    xml = _zip_text(z, 'xl/sharedStrings.xml')
                    text = re.sub(r'<[^>]+>', ' ', xml)
                    return re.sub(r'\s{2,}', ' ', text).strip()[:max_chars]
    except Exception:
        return ''
    return ''


# ============================================================
# OpenAI 兼容调用
# ============================================================

def _chat(messages, cfg):
    url = cfg['base_url'].rstrip('/')
    if not url.endswith('/chat/completions'):
        url += '/chat/completions'
    body = json.dumps({
        'model': cfg['model'],
        'messages': messages,
        'temperature': float(cfg.get('temperature', 0.2)),
        'stream': False,
    }).encode('utf-8')

    req = urllib.request.Request(url, data=body, method='POST')
    req.add_header('Content-Type', 'application/json')
    key = (cfg.get('api_key') or '').strip()
    if key:
        req.add_header('Authorization', 'Bearer ' + key)

    with urllib.request.urlopen(req, timeout=float(cfg.get('timeout', 120))) as r:
        data = json.loads(r.read().decode('utf-8', 'ignore'))

    try:
        return data['choices'][0]['message']['content']
    except (KeyError, IndexError, TypeError):
        # 不回显响应体：避免把内网接口/云元数据的返回内容带到错误提示里。
        raise ValueError('返回格式不符合 OpenAI 协议（缺少 choices[0].message.content）')


def _extract_json_array(text):
    """模型常把 JSON 包在 ``` 里或加前后废话，这里做容错提取。"""
    if not text:
        return None
    t = text.strip()
    t = re.sub(r'^```(?:json)?\s*', '', t)
    t = re.sub(r'\s*```$', '', t)
    try:
        v = json.loads(t)
        if isinstance(v, list):
            return v
        if isinstance(v, dict):
            for k in ('names', 'result', 'items', 'data'):
                if isinstance(v.get(k), list):
                    return v[k]
    except Exception:
        pass
    m = re.search(r'\[[\s\S]*\]', t)
    if m:
        try:
            v = json.loads(m.group(0))
            if isinstance(v, list):
                return v
        except Exception:
            pass
    return None


def suggest(stems, instruction='', cfg=None, with_content=False, paths=None):
    """
    给一批主名（不含扩展名）提新名。

    stems: ['xxx', 'yyy']  已经过规则清洗的名字
    返回: {'ok': bool, 'names': [...], 'error': str, 'raw': str}
    names 与输入等长；某一项模型没给就用原名占位。
    """
    cfg = cfg or load_conf()
    if not is_ready(cfg):
        return {'ok': False, 'error': '尚未启用或未配置模型', 'names': list(stems)}
    if not isinstance(stems, list):
        return {'ok': False, 'error': 'stems 必须是数组', 'names': []}
    if len(stems) > 200:
        return {'ok': False, 'error': '一次最多处理 200 条，请分批操作',
                'names': list(stems)}

    bs = max(1, min(100, int(cfg.get('batch_size', 30))))
    out = list(stems)
    errors = []
    skipped_paths = []

    def _content_allowed(path):
        """只允许读取已授权目录中的文件；LLM 层不能绕过目录白名单。"""
        try:
            p = os.path.abspath(path)
            return (not engine.is_path_blocked(p)
                    and engine.check_root_allowed(os.path.dirname(p))[0])
        except Exception:
            return False

    for start in range(0, len(stems), bs):
        chunk = stems[start:start + bs]
        payload = []
        for i, s in enumerate(chunk):
            item = {'序号': start + i + 1, '原名': s}
            if with_content and paths and (start + i) < len(paths):
                p = paths[start + i]
                if _content_allowed(p):
                    snip = read_snippet(p)
                    if snip:
                        item['内容摘要'] = snip[:300]
                else:
                    skipped_paths.append(p)
            payload.append(item)

        user = '待处理清单（JSON）：\n%s\n\n' % json.dumps(payload, ensure_ascii=False)
        if instruction.strip():
            user += '本次额外要求：%s\n\n' % instruction.strip()
        user += '请输出 JSON 数组，长度必须为 %d。' % len(chunk)

        try:
            content = _chat([
                {'role': 'system', 'content': cfg['prompt']},
                {'role': 'user', 'content': user},
            ], cfg)
            arr = _extract_json_array(content)
            if arr is None:
                errors.append('第 %d 批：模型未返回可解析的 JSON 数组' % (start // bs + 1))
                continue
            for i in range(len(chunk)):
                if i < len(arr) and isinstance(arr[i], str) and arr[i].strip():
                    out[start + i] = arr[i].strip()
                elif i < len(arr) and isinstance(arr[i], dict):
                    v = arr[i].get('新名') or arr[i].get('name')
                    if isinstance(v, str) and v.strip():
                        out[start + i] = v.strip()
        except urllib.error.HTTPError as e:
            errors.append('模型接口返回 HTTP %s' % e.code)
        except urllib.error.URLError as e:
            errors.append('无法连接模型接口：%s' % (e.reason,))
        except (ConnectionError, TimeoutError):
            errors.append('无法连接模型接口或连接中断')
        except Exception as e:
            errors.append('%s: %s' % (type(e).__name__, e))

    result = {
        'ok': not errors,
        'names': out,
        'error': '；'.join(errors[:3]),
    }
    if skipped_paths:
        result['skipped_content'] = skipped_paths[:8]
        result['notice'] = '%d 个文件不在授权目录，未读取内容' % len(skipped_paths)
    return result
