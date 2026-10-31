# -*- coding: utf-8 -*-
"""
Windows 文件名合法性校验与修复。
所有判定都以 Windows 实际行为为准，不照抄 POSIX。
"""

import re

# Windows 保留字符（出现在文件名中即失败）
ILLEGAL_CHARS = set('\\/:*?"<>|')
# 额外：控制字符 0x00-0x1F
MAX_NAME_UTF16 = 255

RESERVED_NAMES = (
    {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$', 'CLOCK$'}
    | {'COM%d' % i for i in range(0, 10)}
    | {'LPT%d' % i for i in range(0, 10)}
    | {'COM' + c for c in '¹²³'}             # Windows 也保留上标形式
    | {'LPT' + c for c in '¹²³'}
)

# 常见替换策略：非法字符替换成什么
REPLACE_CHAR = '-'


def illegal_chars_in(name):
    """列出名称中的非法字符。"""
    bad = []
    for ch in name:
        if ch in ILLEGAL_CHARS:
            bad.append(ch)
        elif ord(ch) < 32:
            bad.append('\\x%02x' % ord(ch))
        elif 0xD800 <= ord(ch) <= 0xDFFF:
            bad.append('孤立代理项')
    return bad


def is_reserved(name):
    """是否为 DOS 设备保留名（CON、CON.txt 都算）。"""
    stem = name.split('.')[0].strip().upper()
    return stem in RESERVED_NAMES


def utf16_len(s):
    # surrogatepass：孤立代理对来自畸形 JSON 时也必须能量长度并判定非法，
    # 不能让 encode 抛 UnicodeEncodeError 把整个预览/执行变成 500。
    return len(s.encode('utf-16-le', 'surrogatepass')) // 2


def check(name, max_len=MAX_NAME_UTF16):
    """
    校验单个文件名（不含路径）。
    返回 (ok: bool, reason: str)
    """
    if not name or not name.strip():
        return False, '文件名为空'

    bad = illegal_chars_in(name)
    if bad:
        return False, '含 Windows 非法字符：%s' % ' '.join(bad)

    if is_reserved(name):
        return False, '与系统保留名冲突（CON/PRN/AUX/NUL/COM1-9/LPT1-9）'

    if name != name.rstrip(' .'):
        return False, '结尾不能是空格或句点'

    if name.startswith(' '):
        return False, '开头不能是空格'

    n = utf16_len(name)
    if n > max_len:
        return False, '名称过长（%d / %d 字符）' % (n, max_len)

    return True, ''


def sanitize(name, replace=REPLACE_CHAR):
    """把非法字符替换掉，返回可用的名字。不保证唯一。"""
    out = []
    for ch in name:
        if ch in ILLEGAL_CHARS or ord(ch) < 32:
            out.append(replace)
        else:
            out.append(ch)
    s = ''.join(out)
    s = s.strip(' .')
    s = re.sub(r'[%s]{2,}' % re.escape(replace), replace, s)
    if not s:
        s = 'untitled'
    if is_reserved(s):
        s = '_' + s
    # 截断到安全长度
    if utf16_len(s) > MAX_NAME_UTF16:
        while s and utf16_len(s) > MAX_NAME_UTF16:
            s = s[:-1]
        # 上面可能切在 emoji 中间留下残缺代理对，encode/decode 会把孤立代理丢掉
        s = s.encode('utf-16-le', 'ignore').decode('utf-16-le', 'ignore')
        s = s.rstrip(' .')
    return s


def split_ext(filename):
    """
    拆分主名与扩展名。
    注意 .tar.gz 这类复合扩展名按最后一个点拆，符合 Windows 习惯。
    隐藏文件 .gitignore 视为主名，无扩展名。
    """
    if filename in ('.', '..'):
        return filename, ''
    if filename.startswith('.') and filename.count('.') == 1:
        return filename, ''
    idx = filename.rfind('.')
    if idx <= 0:
        return filename, ''
    return filename[:idx], filename[idx:]
