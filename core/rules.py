# -*- coding: utf-8 -*-
"""
规则管线。

每条规则作用于"主名"（不含扩展名）。顺序固定，因为规则之间有依赖：
先统一字形 -> 再转数字 -> 再清洗符号 -> 再重排 -> 最后收尾。

对外只有两个入口：
    RULE_LIST   规则清单（给前端渲染开关）
    apply_pipeline(stem, enabled, params, ctx) -> (new_stem, applied, notes)
"""

import os
import re

from . import cn_num, text_norm


# ============================================================
# 日期识别
# ============================================================

_MONTH_DAYS = [31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]


def _valid(y, m, d):
    if not (1 <= m <= 12):
        return False
    if d is None:
        return True
    if not (1 <= d <= 31):
        return False
    md = _MONTH_DAYS[m - 1]
    if m == 2 and y and not (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)):
        md = 28
    return d <= md


# 从强到弱：完整年月日 -> 紧凑 8 位 -> 年月 -> 月日
_DATE_PATTERNS = [
    # 中文完整日期；日/号后紧跟时间数字也要把“日”消费掉，不能留下孤儿“日9时”
    (re.compile(r'(?<!\d)((?:19|20)\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})'
                r'(?:\s*[日号]|\s*(?!\d))'), 'ymd'),
    (re.compile(r'(?<!\d)((?:19|20)\d{2})\s*[-_./]\s*(\d{1,2})\s*[-_./]\s*(\d{1,2})'
                r'(?!\d)'), 'ymd'),
    (re.compile(r'(?<!\d)((?:19|20)\d{2})(\d{2})(\d{2})(?!\d)'), 'ymd'),
    # 年月：“2026年10个”不是月份；无效完整日期的前缀也不能被单独吃掉
    (re.compile(r'(?<!\d)((?:19|20)\d{2})\s*年\s*(\d{1,2})\s*月(?:份)?'
                r'(?!\s*(?:\d|[日号]))'), 'ym'),
    (re.compile(r'(?<!\d)((?:19|20)\d{2})\s*[-_./]\s*(\d{1,2})'
                r'(?!\s*[-_./]\s*\d)'), 'ym'),
    (re.compile(r'(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]'), 'md'),
]


def find_date(text):
    """
    在文本里找日期。返回 (match_start, match_end, y, m, d, raw) 或 None。
    多个候选时取出现位置最早的一个；同一位置取覆盖更长的那一个。
    """
    hits = []
    for pat, kind in _DATE_PATTERNS:
        for m in pat.finditer(text):
            if kind == 'ymd':
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                if _valid(y, mo, d):
                    hits.append((m.start(), m.end(), y, mo, d, m.group(0)))
            elif kind == 'ym':
                y, mo = int(m.group(1)), int(m.group(2))
                if _valid(y, mo, None):
                    hits.append((m.start(), m.end(), y, mo, None, m.group(0)))
            else:  # md，无年份
                # 完整日期校验失败时不能退化成“月日”再用文件年份硬凑。
                prefix = text[:m.start()]
                if re.search(r'(?:19|20)\d{2}\s*[年\-_./]\s*$', prefix):
                    continue
                mo, d = int(m.group(1)), int(m.group(2))
                if _valid(None, mo, d):
                    hits.append((m.start(), m.end(), None, mo, d, m.group(0)))
    if not hits:
        return None
    hits.sort(key=lambda h: (h[0], -(h[1] - h[0])))
    start, end, y, mo, d, raw = hits[0]
    return start, end, y, mo, d, raw


def format_date(y, m, d, style='dash'):
    if style == 'compact':
        s = '%04d%02d%02d' % (y, m, d) if d else '%04d%02d' % (y, m)
        return s
    if style == 'cn':
        return ('%04d年%02d月' % (y, m)) + ('%02d日' % d if d else '')
    # dash
    if d is None:
        return '%04d-%02d' % (y, m)
    return '%04d-%02d-%02d' % (y, m, d)


# ============================================================
# 正则替换（对标 PowerRename / KRename 的 Find & Replace）
# ============================================================

def _r_regex(stem, p, ctx):
    """
    查找替换。替换串支持两种捕获组写法：$1 和 \\1 都认（PowerRename 用 $1）。
    """
    pat = (p.get('pattern') or '').strip()
    if not pat:
        return stem
    flags = re.I if p.get('ignore_case', True) else 0
    try:
        rx = re.compile(pat, flags)
    except re.error as e:
        ctx.setdefault('errors', []).append('正则表达式有误：%s' % e)
        return stem

    repl = p.get('repl') or ''
    # $1 -> \1，但已经写成 \1 的保持不动
    repl = re.sub(r'\$(\d+)', r'\\g<\1>', repl)
    # 具名分组 (?P<x>) 也要能用 $<x> / ${x}
    repl = re.sub(r'\$\{(\w+)\}', r'\\g<\1>', repl)
    repl = re.sub(r'\$<(\w+)>', r'\\g<\1>', repl)
    # 用户输入的是“字面替换串”，反斜杠不能被 re.sub 当控制字符。
    # 保留显式反向引用 \1..\9 / \g<name>，其余 \x 转义成字面反斜杠。
    repl = re.sub(r'\\(?![1-9]|g<)', r'\\\\', repl)

    count = 0 if p.get('all', True) else 1
    try:
        new = rx.sub(repl, stem, count=count)
    except re.error as e:
        ctx.setdefault('errors', []).append('替换执行失败：%s' % e)
        return stem
    if new != stem:
        ctx.setdefault('regex_hits', 0)
        hits = len(rx.findall(stem))
        ctx['regex_hits'] += hits if p.get('all', True) else min(1, hits)
    return new


# ============================================================
# 数字补零（对标拖把更名器的"补齐"）
# ============================================================

def _to_int(v, default):
    """
    安全转 int。
    不能写成 int(v or default)：用户把起始值填 0 时会被 or 吞成默认值。
    """
    if v is None or v == '':
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _char_at(s, i):
    """取第 i 个字符，越界返回空串。负数从末尾算。"""
    s = str(s)
    try:
        i = int(i)
    except (TypeError, ValueError):
        return ''
    if not s or not (-len(s) <= i < len(s)):
        return ''
    return s[i]


def _is_year(s):
    return len(s) == 4 and s[:2] in ('19', '20')


def _r_pad_num(stem, p, ctx):
    """
    把数字补齐到固定位数，解决资源管理器按名称排序时 1、10、2 乱序的问题。
    默认只补主名末尾的数字（最安全）；年份 2026 之类不补。
    """
    width = max(1, min(10, _to_int(p.get('digits'), 3)))
    mode = p.get('mode', 'tail')

    def _pad(s):
        if _is_year(s) or len(s) >= width:
            return s
        return s.zfill(width)

    if mode == 'tail':
        m = re.search(r'(\d+)$', stem)
        if not m:
            return stem
        s = _pad(m.group(1))
        return stem if s == m.group(1) else stem[:m.start()] + s

    if mode == 'first':
        m = re.search(r'\d+', stem)
        if not m:
            return stem
        s = _pad(m.group(0))
        return stem if s == m.group(0) else stem[:m.start()] + s + stem[m.end():]

    return re.sub(r'\d+', lambda m: _pad(m.group(0)), stem)


# ============================================================
# 以分隔符为界（对标拖把更名器的"删除与保留"）
# ============================================================

def _r_boundary(stem, p, ctx):
    """按分隔符切一刀，保留或删掉某一侧。"""
    sep = str(p.get('sep') or '-')       # 清洗后通常已没有“ - ”两侧空格
    mode = p.get('mode', 'keep_before')
    use_last = bool(p.get('last'))
    idx = stem.rfind(sep) if use_last else stem.find(sep)
    if idx < 0:
        return stem

    before = stem[:idx]
    after = stem[idx + len(sep):]

    if mode in ('keep_before', 'drop_after'):
        return before.strip() or stem
    if mode in ('keep_after', 'drop_before'):
        return after.strip() or stem
    return stem


# ============================================================
# 按位置编辑（对标 Ant Renamer 的 Insert / Delete）
# ============================================================

def _r_insert_del(stem, p, ctx):
    """按位置插入、删除、截断。位置支持负数（从末尾算）。"""
    mode = p.get('mode', 'insert')
    text = p.get('text') or ''

    if mode == 'insert':
        pos = _to_int(p.get('pos'), 0)
        if pos < 0:
            pos = max(0, len(stem) + pos + 1)
        pos = min(pos, len(stem))
        return stem[:pos] + text + stem[pos:]

    if mode == 'delete':
        pos = _to_int(p.get('pos'), 0)
        cnt = _to_int(p.get('count'), 1)
        if cnt <= 0:
            return stem
        if p.get('from_end'):
            # max(0,...) 防止 cnt 超过长度时切片变成"从头砍"
            return stem[:max(0, len(stem) - cnt)]
        start = pos if pos >= 0 else max(0, len(stem) + pos)
        return (stem[:start] + stem[start + cnt:]) or stem

    if mode == 'truncate':
        cnt = max(1, min(500, _to_int(p.get('keep'), 20)))
        return stem[:cnt]

    if mode == 'keep_tail':
        cnt = max(1, min(500, _to_int(p.get('keep'), 20)))
        return stem[-cnt:]

    return stem


# ============================================================
# 大小写（含 KRename 的 Title Case）
# ============================================================

def _r_case(stem, p, ctx):
    mode = p.get('mode', 'lower')
    if mode == 'lower':
        return stem.lower()
    if mode == 'upper':
        return stem.upper()
    if mode == 'title':
        # 每个英文词首字母大写，中文不受影响
        return re.sub(r"[A-Za-z][A-Za-z']*",
                      lambda m: m.group(0)[0].upper() + m.group(0)[1:].lower(), stem)
    if mode == 'first':
        return stem[:1].upper() + stem[1:] if stem else stem
    return stem


# ============================================================
# 无意义噪声词
# ============================================================

# 内置默认词库。用户可在界面上改，改后存到 data/noise.txt。
# 英文词不用写大小写变体：匹配时统一加了 re.IGNORECASE
DEFAULT_NOISE = [
    '副本', '拷贝', '复件', '新建', '未命名', '無標題', '无标题',
    '最终版', '最终版本', '修改版', '最新版', '定稿版', '修订版',
    '新建文件夹', '临时文件', '未命名文件',
    'final', 'copy', 'temp', 'tmp',
]

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOISE_FILE = os.path.join(_BASE, 'data', 'noise.txt')


def load_noise_words():
    """
    读用户词库；只有文件不存在时才用内置默认。
    文件存在但内容为空 = 用户明确清空了词库，不能偷偷退回默认。
    """
    try:
        if os.path.exists(NOISE_FILE):
            with open(NOISE_FILE, 'r', encoding='utf-8') as f:
                txt = f.read()
            return [w.strip() for w in re.split(r'[\s,，、;；]+', txt) if w.strip()]
    except Exception:
        pass
    return list(DEFAULT_NOISE)


def save_noise_words(words):
    """存成一行一个，避免超长行不好读。"""
    os.makedirs(os.path.dirname(NOISE_FILE), exist_ok=True)
    clean = [w.strip() for w in words if w and w.strip()]
    with open(NOISE_FILE, 'w', encoding='utf-8') as f:
        f.write('\n'.join(clean) + '\n')
    return clean


def _noise_pattern(w):
    """
    英文/数字开头的词，要求两侧不能紧挨其它字母数字，否则 temp 会把 template 里的
    temp 吃掉。注意不能用 \\b：Python 的 \\w 把汉字也算单词字符，
    "temp文件" 里的 \\b 不成立，会导致该删的删不掉。
    """
    if re.match(r'^[A-Za-z0-9]', w):
        return r'(?<![A-Za-z0-9])' + re.escape(w) + r'(?![A-Za-z0-9])'
    return re.escape(w)


_NOISE_RE_CACHE = {}


def get_noise_regex():
    words = load_noise_words()
    key = tuple(words)
    if key in _NOISE_RE_CACHE:
        return _NOISE_RE_CACHE[key]
    if len(_NOISE_RE_CACHE) > 8:
        _NOISE_RE_CACHE.clear()
    if not words:
        _NOISE_RE_CACHE[key] = None
    else:
        # IGNORECASE 让 final 同时覆盖 Final/FINAL；对中文词没有副作用
        _NOISE_RE_CACHE[key] = re.compile(
            '|'.join(_noise_pattern(w) for w in sorted(words, key=len, reverse=True)),
            re.IGNORECASE)
    return _NOISE_RE_CACHE[key]


# 复制产生的 (1) (2) _1 后缀
_COPY_SUFFIX_RE = re.compile(r'[\s_\-]*\((\d{1,2})\)\s*$|[\s_\-]+副本\s*$|[\s_\-]+copy\s*$', re.I)


def _strip_noise(stem):
    rx = get_noise_regex()
    s = rx.sub('', stem) if rx else stem
    s = _COPY_SUFFIX_RE.sub('', s)
    s = re.sub(r'\s{2,}', ' ', s)
    return s.strip(' _-')


# ============================================================
# 规则实现
# ============================================================

def _r_t2s(stem, p, ctx):
    return text_norm.to_simplified(stem)


def _r_fullwidth(stem, p, ctx):
    return text_norm.to_halfwidth(stem)


def _r_cn_num(stem, p, ctx):
    new, changes = cn_num.convert_text(
        stem,
        unit_read=True,
        per_digit=p.get('per_digit', True),
        min_per_len=_to_int(p.get('min_per_len'), 3),
        big_unit=p.get('big_unit', 'cn'),
    )
    if changes:
        ctx.setdefault('numbers', []).extend(changes)
    return new


def _r_noise(stem, p, ctx):
    removed = stem
    new = _strip_noise(stem)
    if new != removed and new:
        ctx.setdefault('noise', []).append(removed)
    return new or removed


def _r_clean(stem, p, ctx):
    level = p.get('level', 'normal')
    out = text_norm.clean_symbols(stem, level)
    # 符号全被清掉（如 ★、---）时必须退回原名，否则产生空主名
    return out or stem


def _r_rare(stem, p, ctx):
    """
    生僻字处理。
    GB2312 之外的汉字在部分老系统/传输链路上会变成方框，
    默认只检测提示，要真删得显式开启。
    """
    mode = p.get('mode', 'check')
    rare = text_norm.find_rare_chars(stem)
    if not rare:
        return stem
    ctx['rare'] = rare
    if mode == 'check':
        return stem
    if mode == 'mark':
        return ''.join('_' if text_norm.is_rare_char(c) else c for c in stem)
    if mode == 'remove':
        out = ''.join('' if text_norm.is_rare_char(c) else c for c in stem)
        return out or stem        # 全被删光就退回原名，避免产生空名
    return stem


def _r_date_front(stem, p, ctx):
    style = p.get('style', 'dash')
    sep = p.get('sep', '_')
    hit = find_date(stem)
    if not hit:
        return stem
    start, end, y, m, d, raw = hit

    # 没有年份的"10月29日"，用文件修改时间补年份
    if y is None:
        y = ctx.get('year')
        # 取不到有效年份就别硬拼：format_date 里 %04d 遇到空串会抛 TypeError
        if not isinstance(y, int):
            return stem

    # 月日识别时 2 月 29 日会先放行，补上年份后必须按该年重新验闰
    if not _valid(y, m, d):
        return stem
    date_str = format_date(y, m, d, style)

    rest = (stem[:start] + ' ' + stem[end:]).strip()
    rest = re.sub(r'\s+', ' ', rest)
    rest = text_norm.strip_edges(rest)

    ctx['date'] = date_str
    ctx['date_raw'] = raw

    if not rest:
        return date_str
    return date_str + sep + rest


def _r_trim(stem, p, ctx):
    s = text_norm.strip_edges(stem)
    # 清洗后常留下空的 () [] {}，直接抹掉
    s = re.sub(r'\(\s*\)|\[\s*\]|\{\s*\}', '', s)
    s = re.sub(r'\s+', ' ', s)
    s = re.sub(r'[-_]{2,}', '-', s)
    return text_norm.strip_edges(s) or stem      # 不能返回空主名


def _r_seq(stem, p, ctx):
    """
    序号。对标 KRename 的 numbering(start, steps)。
    reset_per_dir 打开后，序号在每个子目录里从 start 重新计数。
    """
    start = max(0, min(999999, _to_int(p.get('start'), 1)))
    step = max(1, min(999, _to_int(p.get('step'), 1)))
    sep = str(p.get('sep') or '_')
    pos = p.get('pos', 'prefix')
    total = max(1, _to_int(ctx.get('total'), 1))

    idx = max(0, _to_int(ctx.get('dir_index'), 0)) if p.get('reset_per_dir') \
        else max(0, _to_int(ctx.get('index'), 0))
    n = start + idx * step

    digits = max(0, min(6, _to_int(p.get('digits'), 0)))
    if not digits:
        digits = max(2, len(str(start + max(0, total - 1) * step)))
    s = '%0*d' % (int(digits), n)
    return (s + sep + stem) if pos == 'prefix' else (stem + sep + s)


def _r_affix(stem, p, ctx):
    pre = p.get('prefix', '') or ''
    suf = p.get('suffix', '') or ''
    return pre + stem + suf


def _slice_val(s, a, b):
    s = str(s)
    try:
        i = int(a) if a not in ('', None) else 0
    except ValueError:
        i = 0
    try:
        j = int(b) if b not in ('', None) else len(s)
    except ValueError:
        j = len(s)
    return s[i:j]


def _r_template(stem, p, ctx):
    """
    整名模板。

    变量：{name} {old} {date} {seq} {ext} {parent} {grandparent}
          {mtime} {mtime_full} {ctime} {year} {month} {day}
          {size} {size_kb} {size_mb} {index}
    子串（对标 KRename 的 "using substrings of the old filename"）：
          {name[0:4]}  取第 1-4 个字符
          {name[:6]}   取前 6 个
          {name[3:]}   去掉前 3 个
          {name[-4:]}  取最后 4 个
          {name[2]}    取第 3 个字符
    """
    tpl = p.get('template', '') or ''
    if not tpl.strip():
        return stem

    total = _to_int(ctx.get('total'), 1)
    vals = {
        'name': stem,
        'old': ctx.get('old_stem', stem),
        'date': ctx.get('date', ''),
        'ext': (ctx.get('ext') or '').lstrip('.'),
        'parent': ctx.get('parent', ''),
        'grandparent': ctx.get('grandparent', ''),
        'mtime': ctx.get('mtime_str', ''),
        'mtime_full': ctx.get('mtime_full', ''),
        'ctime': ctx.get('ctime_str', ''),
        'year': ctx.get('year', ''),
        'month': ctx.get('month', ''),
        'day': ctx.get('day', ''),
        'size': ctx.get('size', 0),
        'size_kb': ctx.get('size_kb', 0),
        'size_mb': ctx.get('size_mb', 0),
        'index': ctx.get('index', 0) + 1,
        'seq': '%0*d' % (max(2, len(str(total))), ctx.get('index', 0) + 1),
    }

    out = tpl
    # 先换子串，再换普通变量（否则 {name[a:b]} 会被 {name} 提前吃掉）
    out = re.sub(r'\{(\w+)\[(-?\d*):(-?\d*)\]\}',
                 lambda m: _slice_val(vals.get(m.group(1), ''), m.group(2), m.group(3)), out)
    out = re.sub(r'\{(\w+)\[(-?\d+)\]\}',
                 lambda m: _char_at(vals.get(m.group(1), ''), m.group(2)), out)
    for k, v in vals.items():
        out = out.replace('{%s}' % k, str(v))
    return out or stem


# ============================================================
# 清单（顺序即执行顺序）
# ============================================================

RULES = [
    {
        'id': 't2s', 'name': '繁体转简体 + 异体字归一',
        'group': '字符规范', 'default': True,
        'desc': '繁体→简体，峯/羣/喆 等异体字统一为规范写法',
        'params': [], 'fn': _r_t2s,
    },
    {
        'id': 'fullwidth', 'name': '全角转半角',
        'group': '字符规范', 'default': True,
        'desc': 'ＡＢＣ１２３ → ABC123，全角空格→半角',
        'params': [], 'fn': _r_fullwidth,
    },
    {
        'id': 'cn_num', 'name': '中文数字转阿拉伯',
        'group': '字符规范', 'default': True,
        'desc': '一百一十→110、一九九八→1998、壹佰壹拾→110',
        'params': [
            {'key': 'per_digit', 'type': 'bool', 'label': '逐位读法（年份/编号）', 'default': True},
            {'key': 'min_per_len', 'type': 'int', 'label': '逐位最少字数', 'default': 3,
             'min': 2, 'max': 8},
            {'key': 'big_unit', 'type': 'select', 'label': '大数写法', 'default': 'cn',
             'options': [['cn', '用万/亿（110万）'], ['plain', '完整数字（1100000）']]},
        ], 'fn': _r_cn_num,
    },
    {
        'id': 'regex', 'name': '查找替换 / 正则',
        'group': '替换', 'default': False,
        'desc': 'PowerRename 式查找替换，支持正则与 $1 捕获组',
        'params': [
            {'key': 'pattern', 'type': 'text', 'label': '查找', 'default': ''},
            {'key': 'repl', 'type': 'text', 'label': '替换为', 'default': ''},
            {'key': 'ignore_case', 'type': 'bool', 'label': '忽略大小写', 'default': True},
            {'key': 'all', 'type': 'bool', 'label': '替换全部（否则只换第一个）', 'default': True},
            {'key': 'help', 'type': 'note', 'label':
                '例：查找 (\\d{4})-(\\d{2}) 替换 $1年$2月；查找 ^ 替换 prefix_'},
        ], 'fn': _r_regex,
    },
    {
        'id': 'noise', 'name': '去除无意义噪声词',
        'group': '清洗', 'default': True,
        'desc': '删掉「副本/新建/最终版/copy/(1)」等复制残留',
        'action': {'type': 'edit_noise', 'label': '编辑词库'},
        'params': [], 'fn': _r_noise,
    },
    {
        'id': 'clean', 'name': '符号清洗',
        'group': '清洗', 'default': True,
        'desc': '剔除全角特殊符号与表情，统一分隔符',
        'params': [
            {'key': 'level', 'type': 'select', 'label': '强度', 'default': 'normal',
             'options': [['off', '仅处理非法字符'], ['normal', '标准'], ['aggressive', '激进（只留中英文数字）']]},
        ], 'fn': _r_clean,
    },
    {
        'id': 'rare', 'name': '生僻字处理',
        'group': '清洗', 'default': False,
        'desc': 'GB2312 之外的汉字在老系统上易显示为方框，可检出并处理',
        'params': [
            {'key': 'mode', 'type': 'select', 'label': '处理方式', 'default': 'check',
             'options': [['check', '只提示不修改'], ['mark', '替换为 _'], ['remove', '直接删除']]},
        ], 'fn': _r_rare,
    },
    {
        'id': 'boundary', 'name': '按分隔符保留 / 删除',
        'group': '重排', 'default': False,
        'desc': '以某个字符串为界切一刀，保留或删掉一边',
        'params': [
            {'key': 'sep', 'type': 'text', 'label': '分隔符', 'default': '-'},
            {'key': 'mode', 'type': 'select', 'label': '操作', 'default': 'keep_before',
             'options': [['keep_before', '保留分隔符前面'], ['keep_after', '保留分隔符后面'],
                         ['drop_before', '删掉分隔符前面'], ['drop_after', '删掉分隔符后面']]},
            {'key': 'last', 'type': 'bool', 'label': '有多个时取最后一个', 'default': False},
        ], 'fn': _r_boundary,
    },
    {
        'id': 'date_front', 'name': '日期挪到最前面',
        'group': '重排', 'default': True,
        'desc': '「会议纪要2026年10月29日」→「2026-10-29_会议纪要」',
        'params': [
            {'key': 'style', 'type': 'select', 'label': '日期格式', 'default': 'dash',
             'options': [['dash', '2026-10-29'], ['compact', '20261029'], ['cn', '2026年10月29日']]},
            {'key': 'sep', 'type': 'text', 'label': '与标题的分隔符', 'default': '_'},
        ], 'fn': _r_date_front,
    },
    {
        'id': 'trim', 'name': '收尾整理',
        'group': '重排', 'default': True,
        'desc': '清掉清洗后残留的空括号、首尾空格与重复分隔符',
        'params': [], 'fn': _r_trim,
    },
    {
        'id': 'case', 'name': '英文大小写',
        'group': '重排', 'default': False,
        'desc': '把文件名中的英文字母统一大小写',
        'params': [
            {'key': 'mode', 'type': 'select', 'label': '方式', 'default': 'lower',
             'options': [['keep', '保持原样'], ['lower', '全部小写'], ['upper', '全部大写'],
                         ['title', '每个词首字母大写（Title Case）'],
                         ['first', '仅首字母大写']]},
        ], 'fn': _r_case,
    },
    {
        'id': 'insert_del', 'name': '按位置插入 / 删除',
        'group': '重排', 'default': False,
        'desc': '在指定位置插入或删除字符，位置可为负数（从末尾算）',
        'params': [
            {'key': 'mode', 'type': 'select', 'label': '操作', 'default': 'insert',
             'options': [['insert', '插入'], ['delete', '删除'],
                         ['truncate', '只保留前 N 字'], ['keep_tail', '只保留后 N 字']]},
            {'key': 'text', 'type': 'text', 'label': '要插入的文本', 'default': ''},
            {'key': 'pos', 'type': 'int', 'label': '位置（负=从末尾）', 'default': 0,
             'min': -200, 'max': 200},
            {'key': 'count', 'type': 'int', 'label': '删除字符数', 'default': 1, 'min': 1, 'max': 500},
            {'key': 'keep', 'type': 'int', 'label': '保留前/后字数', 'default': 20,
             'min': 1, 'max': 500},
            {'key': 'from_end', 'type': 'bool', 'label': '删除时从末尾算', 'default': False},
        ], 'fn': _r_insert_del,
    },
    {
        'id': 'seq', 'name': '加序号前缀',
        'group': '收尾', 'default': False,
        'desc': '按当前列表顺序编号，可设起始值与步长',
        'params': [
            {'key': 'start', 'type': 'int', 'label': '起始值', 'default': 1, 'min': 0, 'max': 999999},
            {'key': 'step', 'type': 'int', 'label': '步长', 'default': 1, 'min': 1, 'max': 999},
            {'key': 'digits', 'type': 'int', 'label': '位数（0=自动）', 'default': 0, 'min': 0, 'max': 6},
            {'key': 'sep', 'type': 'text', 'label': '分隔符', 'default': '_'},
            {'key': 'pos', 'type': 'select', 'label': '位置', 'default': 'prefix',
             'options': [['prefix', '加在前面'], ['suffix', '加在后面']]},
            {'key': 'reset_per_dir', 'type': 'bool', 'label': '每个子目录重新计数', 'default': False},
        ], 'fn': _r_seq,
    },
    {
        'id': 'pad_num', 'name': '数字补零',
        'group': '收尾', 'default': False,
        'desc': '把数字补齐到固定位数，修掉「1、10、2」的排序错乱',
        'params': [
            {'key': 'digits', 'type': 'int', 'label': '补到几位', 'default': 3, 'min': 1, 'max': 10},
            {'key': 'mode', 'type': 'select', 'label': '补哪些数字', 'default': 'tail',
             'options': [['tail', '只补末尾的数字'], ['first', '只补第一个数字'],
                         ['all', '所有数字（年份除外）']]},
        ], 'fn': _r_pad_num,
    },
    {
        'id': 'affix', 'name': '加前后缀',
        'group': '收尾', 'default': False,
        'desc': '所有名字统一加前缀或后缀',
        'params': [
            {'key': 'prefix', 'type': 'text', 'label': '前缀', 'default': ''},
            {'key': 'suffix', 'type': 'text', 'label': '后缀', 'default': ''},
        ], 'fn': _r_affix,
    },
    {
        'id': 'template', 'name': '整名模板',
        'group': '收尾', 'default': False,
        'desc': '用模板彻底重写。变量 {name} {old} {date} {seq} {ext} {parent} {mtime} '
                '{ctime} {year} {size}，子串 {name[0:4]} {name[-3:]}',
        'params': [
            {'key': 'template', 'type': 'text', 'label': '模板', 'default': '{date}_{name}'},
        ], 'fn': _r_template,
    },
]

_RULE_INDEX = {r['id']: r for r in RULES}
_RULE_ORDER = [r['id'] for r in RULES]


def default_enabled():
    return {r['id']: r['default'] for r in RULES}


def default_params():
    out = {}
    for r in RULES:
        p = {}
        for spec in r['params']:
            if 'default' in spec:          # type=note 的纯说明项没有默认值
                p[spec['key']] = spec['default']
        out[r['id']] = p
    return out


def apply_pipeline(stem, enabled=None, params=None, ctx=None):
    """
    按固定顺序跑规则。
    enabled: {rule_id: bool}
    params:  {rule_id: {key: value}}
    ctx:     上下文（index/total/year/ext/parent），会回填 date、numbers 等
    返回 (new_stem, applied_ids, ctx)
    """
    # 用 is None 判断：{} 表示"全部关闭"，不能被 or 替换成默认值
    if enabled is None:
        enabled = default_enabled()
    if params is None:
        params = {}
    ctx = ctx if ctx is not None else {}

    applied = []
    cur = stem
    for rid in _RULE_ORDER:
        # 只有 enabled=None 才使用默认组合；传入映射后，缺失键就是关闭
        if not enabled.get(rid, False):
            continue
        p = params.get(rid) or {}
        try:
            nxt = _RULE_INDEX[rid]['fn'](cur, p, ctx)
        except Exception as e:                     # 单条规则出错不影响整体
            ctx.setdefault('errors', []).append('%s: %s' % (rid, e))
            continue
        if nxt != cur:
            applied.append(rid)
            cur = nxt
    return cur, applied, ctx


def rule_catalog():
    """给前端的规则清单（剔除函数对象）。"""
    out = []
    for r in RULES:
        item = {
            'id': r['id'], 'name': r['name'], 'group': r['group'],
            'default': r['default'], 'desc': r['desc'], 'params': r['params'],
        }
        if r.get('action'):
            item['action'] = r['action']
        out.append(item)
    return out
