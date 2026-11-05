# -*- coding: utf-8 -*-
"""
文本规范化：繁简、异体字、全角半角、符号清洗、生僻字检测。

zhconv 来自 vendor/ 目录（纯 Python，无需 pip 安装）。
"""

import os
import re
import sys
import unicodedata  # modified: NFC 归一化，来源 Microsoft PowerToys PowerRename 的 SanitizeAndNormalize()

_HERE = os.path.dirname(os.path.abspath(__file__))
_VENDOR = os.path.join(os.path.dirname(_HERE), 'vendor')
if _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)

try:
    import zhconv
    _HAS_ZHCONV = True
except Exception:                                    # pragma: no cover
    zhconv = None
    _HAS_ZHCONV = False

# ---------- 繁简 / 异体 ----------

# zhconv 覆盖不到、或各地方习惯写法不统一的常见异体字
VARIANT_MAP = {
    '峯': '峰', '羣': '群', '啓': '启', '喆': '哲', '佈': '布',
    '遊': '游', '週': '周', '祕': '秘', '汙': '污', '蹟': '迹',
    '麪': '面', '捨': '舍', '蔘': '参', '灑': '洒', '嚮': '向',
    '牠': '它', '妳': '你', '暱': '昵', '儘': '尽', '甦': '苏',
    '囪': '囱', '摀': '捂', '擡': '抬', '廕': '荫', '慾': '欲',
    '懾': '慑', '甯': '宁', '綑': '捆', '絃': '弦', '菸': '烟',
    '蕋': '蕊', '諮': '咨', '貍': '狸', '迺': '乃',
    '鉅': '巨', '銲': '焊', '鍼': '针', '鑪': '炉', '閤': '合',
    '陞': '升', '餚': '肴', '麐': '麟', '麼': '么', '麽': '么',
    '著': '着',   # 仅在"著/着"混用的场景，保守起见保留
}
# "著" 在公文里常作"著"（著作），不做转换更安全
VARIANT_MAP.pop('著', None)


# 这些字常用于人名/姓氏；批量工具不能替用户判断“是不是错字”。
# 用私用区占位符绕过 zhconv 和本地异体表，转换完再还原。
_NAME_SAFE = '喆甯陞鉅迺牠妳'


def to_simplified(text):
    """繁体 -> 简体，并做异体字归一；人名常用字保持原样。"""
    # 来源场景：Microsoft PowerToys PowerRename 的 SanitizeAndNormalize() 明确
    # 「先 NormalizeString(NFC) 再清洗」。用途：NFD 文件名（macOS / 网页拷贝）
    # 里的组合声调符如 U+0301 会被后面的符号清洗当非白名单字符删掉，
    # cafe\u0301报告.pdf 会静默变成 cafe报告.pdf；先合成 café 才不会丢字。
    text = unicodedata.normalize('NFC', text)  # modified
    # 占位符原来固定用 U+E000..E006（BMP 私用区）。若文件名本身含这些码位，
    # 反向还原会把用户原有字符误改成人名用字（U+E000 -> 喆）。
    # 改用辅助平面私用区 U+F0000 起，并跳过原文已占用的码位。
    n = len(_NAME_SAFE)  # modified
    base = 0xF0000  # modified
    while any(chr(base + i) in text for i in range(n)):  # modified
        base += n  # modified
    placeholders = {ch: chr(base + i) for i, ch in enumerate(_NAME_SAFE)}  # modified
    reverse = {v: k for k, v in placeholders.items()}
    protected = ''.join(placeholders.get(c, c) for c in text)
    if _HAS_ZHCONV:
        try:
            protected = zhconv.convert(protected, 'zh-cn')
        except Exception:
            pass
    protected = ''.join(VARIANT_MAP.get(c, c) for c in protected)
    return ''.join(reverse.get(c, c) for c in protected)


def to_traditional(text):
    """简体 -> 繁体（港澳台习惯），失败时原样返回。"""
    if not _HAS_ZHCONV:
        return text
    try:
        return zhconv.convert(text, 'zh-tw')
    except Exception:
        return text


def has_traditional(text):
    """粗略判断是否含繁体字：转简体后与原串不同即有。"""
    return to_simplified(text) != text


# ---------- 全角 / 半角 ----------

# 只转全角 ASCII 区，不用 NFKC（NFKC 会把 ① 变 1、㎏ 变 kg，破坏原意）
def to_halfwidth(text):
    out = []
    for ch in text:
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        elif code == 0x3000:
            out.append(' ')
        else:
            out.append(ch)
    return ''.join(out)


def to_fullwidth(text):
    out = []
    for ch in text:
        code = ord(ch)
        if 0x21 <= code <= 0x7E:
            out.append(chr(code + 0xFEE0))
        elif code == 0x20:
            out.append('\u3000')
        else:
            out.append(ch)
    return ''.join(out)


# ---------- 符号处理 ----------

# 全角标点 -> 半角对应（清洗时统一成半角，视觉更整齐）
# 注意：书名号不能映射成 < >，那是 Windows 非法字符，映射成空更安全
PUNCT_MAP = {
    '，': ',', '。': '.', '！': '!', '？': '?', '；': ';', '：': ':',
    '、': ',', '（': '(', '）': ')', '［': '[', '］': ']',
    '｛': '{', '｝': '}', '【': '[', '】': ']', '《': '', '》': '',
    '“': '"', '”': '"', '‘': "'", '’': "'", '「': '"', '」': '"',
    '『': '"', '』': '"', '〈': '', '〉': '', '～': '~', '－': '-',
    '—': '-', '–': '-', '―': '-', '…': '', '·': '', '•': '',
    '※': '', '★': '', '☆': '', '◆': '', '■': '', '□': '',
    '●': '', '◎': '', '▶': '', '►': '', '→': '-',
    '←': '-', '↑': '', '↓': '', '　': ' ',
}

# 文件名里允许保留的符号（半角）
KEEP_SYMBOLS = set('-_ +()[]{}.,!@#$%^&~=\'')
# 注意：Windows 非法字符 \ / : * ? " < > | 单独处理


def is_decor_char(ch):
    """
    装饰性字符：带圈数字、制表符、几何图形、emoji、私用区、零宽字符。
    这些字符 isalnum() 有时返回 True（如 ①），单靠 alnum 判断会漏。

    例外：○(U+25CB) 要留着——它是中文数字「零」的常用写法（二○二六），
    删掉会把数字变成错的。
    """
    code = ord(ch)
    return (
        0x2460 <= code <= 0x24FF or      # ① ⑴ ⒈ ⒜
        0x2500 <= code <= 0x257F or      # 制表符 ─│┌
        (0x2580 <= code <= 0x25FF and code != 0x25CB) or   # 方块/几何 ■ ▲ ◆（○ 除外）
        0x2600 <= code <= 0x27BF or      # 杂项符号 ☀ ★ ♥ ➜
        0x2B00 <= code <= 0x2BFF or      # ⬛ ⭐
        0x1F000 <= code <= 0x1FAFF or    # emoji
        0xE000 <= code <= 0xF8FF or      # 私用区
        0xFE00 <= code <= 0xFE0F or      # 变体选择符
        0x202A <= code <= 0x202E or      # 双向文字覆盖（可伪装扩展名）
        0x2066 <= code <= 0x2069 or      # 双向文字隔离
        code in (0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF)  # 零宽/方向控制
    )


def is_cjk(ch):
    """
    是否汉字。注意不能只判断 4E00-9FFF：
    〇(U+3007) 是中文数字"零"，扩展 A 区和兼容表意区也都是汉字。
    """
    code = ord(ch)
    return (0x4E00 <= code <= 0x9FFF        # 基本区
            or 0x3400 <= code <= 0x4DBF     # 扩展 A
            or 0x20000 <= code <= 0x323AF   # 扩展 B 及后续各区
            or 0xF900 <= code <= 0xFAFF     # 兼容表意文字
            or 0x2F800 <= code <= 0x2FA1F   # 兼容表意文字补充
            or code == 0x3007)              # 〇


_WIN_ILLEGAL = set('\\/:*?"<>|')


def clean_symbols(text, level='normal'):
    """
    符号清洗。
      off       只把 Windows 非法字符换成 -，其余（含装饰字符）一律不动
      normal    全角标点转半角、去掉装饰性符号、合并多余分隔符
      aggressive 只保留中文 + 字母数字 + - _ . ( )
    """
    if level == 'off':
        # 最小干预。装饰字符是否清理由用户选 normal/aggressive 决定，off 不擅自删
        return ''.join('-' if (c in _WIN_ILLEGAL or ord(c) < 32) else c for c in text)

    # ○(U+25CB) 是中文数字"零"的常见写法（二○二六），
    # 它既不是 alnum 也不在符号白名单里，会被下面的过滤丢掉，把数字变成错的。
    # 先归一成汉字"〇"，后续逻辑自然就能保住它。
    text = text.replace('○', '〇')

    # 装饰性字符一律清掉（所有档位都清：①、★、emoji 在文件名里没有语义）
    text = ''.join(ch for ch in text if not is_decor_char(ch))

    out = []
    for ch in text:
        if ch in PUNCT_MAP:
            out.append(PUNCT_MAP[ch])
        else:
            out.append(ch)
    text = ''.join(out)
    # Windows 非法字符是“结构分隔符”，必须替换成 -，不能直接删除。
    # 否则 10:30 会粘成 1030、a|b 会变成 ab，既改语义又制造重名。
    text = ''.join('-' if (ch in _WIN_ILLEGAL or ord(ch) < 32) else ch
                   for ch in text)

    if level == 'aggressive':
        kept = []
        for ch in text:
            if is_cjk(ch):
                kept.append(ch)
            elif ch.isdigit() or ('a' <= ch <= 'z') or ('A' <= ch <= 'Z'):
                kept.append(ch)
            elif ch in '-_.()':
                kept.append(ch)
            elif ch in ' \t':
                kept.append(' ')
            # 其余丢弃
        text = ''.join(kept)
    else:
        # normal：删掉剩下的符号类字符（保留字母、数字、常用连接符）
        kept = []
        for ch in text:
            if ch.isalnum() or is_cjk(ch):
                kept.append(ch)
            elif ch in KEEP_SYMBOLS:
                kept.append(ch)
            elif ch.isspace():
                kept.append(' ')
            # 其余（表情、控制符、罕见标点）丢弃
        text = ''.join(kept)

    # 合并连续分隔符与空白
    text = re.sub(r'\s+', ' ', text)
    text = re.sub(r'[-_]{2,}', '-', text)
    text = re.sub(r'\s*([-_])\s*', r'\1', text)
    # 结尾点号在 Windows 上是非法的（「报告。」转成「报告.」就不能用了），
    # 收尾必须在规则层就做掉，不能依赖后面 trim 是否开启
    return text.strip().rstrip(' .')


# ---------- 生僻字 ----------

_GB2312_CACHE = {}


def is_rare_char(ch):
    """
    是否属于"非常用字"。
    判据：GB2312 编码失败 => 不在 6763 个常用汉字表中。
    注意这是近似判断，康熙部首、兼容汉字区也算。
    """
    if ch in _GB2312_CACHE:
        return _GB2312_CACHE[ch]
    if not is_cjk(ch):
        _GB2312_CACHE[ch] = False
        return False
    # 〇 是中文数字"零"，GB2312 收不进去但不是生僻字，绝不能被删掉或替换成 _
    if ord(ch) == 0x3007:
        _GB2312_CACHE[ch] = False
        return False
    try:
        ch.encode('gb2312')
        rare = False
    except UnicodeEncodeError:
        rare = True
    _GB2312_CACHE[ch] = rare
    return rare


def find_rare_chars(text):
    """返回文本中检测到的生僻字列表（去重、保序）。"""
    seen = []
    for ch in text:
        if is_rare_char(ch) and ch not in seen:
            seen.append(ch)
    return seen


# ---------- 通用小工具 ----------

def normalize_spaces(text):
    return re.sub(r'\s+', ' ', text).strip()


def strip_edges(text):
    """去掉首尾的空格、点、连字符、下划线（Windows 不允许结尾为空格或点）。"""
    return text.strip(' .-_·-　\t')


def utf16_len(s):
    """Windows 文件名长度按 UTF-16 code unit 计。"""
    return len(s.encode('utf-16-le')) // 2
