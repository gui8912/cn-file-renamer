# -*- coding: utf-8 -*-
"""
中文数字 -> 阿拉伯数字

三种读法分开处理，互不干扰：

1. 数值读法（有权位单位）
       一百一十 -> 110      三百零五 -> 305
       一万二   -> 12000    两千三百 -> 2300
       壹佰壹拾 -> 110      十       -> 10
   口语省略补位：一百一 = 110（省了"十"）、一千五 = 1500、一万二 = 12000
   但有"零"占位时不补位：一百零一 = 101、一千零五 = 1005

2. 逐位读法（年份 / 编号，无单位）
       一九九八 -> 1998     二〇二六 -> 2026
   要求连续 >= 3 个字，避免"二三"这种歧义串被误伤

3. 小数
       三点一四 -> 3.14

刻意保守：绝不替换孤立单字，所以"第一次"不会变成"第1次"。
例外：可选开启 ordinal_single，只处理「第 + 单字数字 + 编号量词」这种
明确编号语境（第一部分 -> 第1部分、第三章 -> 第3章），默认关闭。
"""

import re

# ---------- 字典 ----------

DIGITS = {
    '零': 0, '〇': 0, '○': 0,
    '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5,
    '六': 6, '七': 7, '八': 8, '九': 9,
    '壹': 1, '贰': 2, '叁': 3, '肆': 4, '伍': 5,
    '陆': 6, '柒': 7, '捌': 8, '玖': 9,
    '幺': 1,
}
UNITS = {'十': 10, '拾': 10, '百': 100, '佰': 100, '千': 1000, '仟': 1000}
BIG = {'万': 10000, '萬': 10000, '亿': 100000000, '億': 100000000}

ALL_CHARS = set(DIGITS) | set(UNITS) | set(BIG)

DIGIT_CLS = ''.join(DIGITS)
UNIT_CLS = ''.join(UNITS)
BIG_CLS = ''.join(BIG)

# 数值读法：至少 2 个中文字，必须含单位才处理（见 _convert_unit_read）
NUM_RUN = re.compile('[' + DIGIT_CLS + UNIT_CLS + BIG_CLS + ']{2,}')
# 小数：「点」前必须至少有一个数字字，否则「观点一」「重点二」会被误转成 观0.1 / 重0.2
DECIMAL_RUN = re.compile('[' + DIGIT_CLS + UNIT_CLS + BIG_CLS + ']+[点點][' + DIGIT_CLS + ']+')
# 逐位读法：先按 2 字起匹配，真正的最少字数由 min_per_len 决定（否则设成 2 永远不生效）
PER_RUN = re.compile('[' + DIGIT_CLS + ']{2,}')
# 日历里的单字月/日：“一月五日”中的“一/五”不在 NUM_RUN（要求≥2字）里。
# 负向后顾避免把“十一月”的第二个字单独换掉，产出“十1月”。
CAL_SINGLE = re.compile(
    r'(?<![一二三四五六七八九十百千万亿〇零])'
    r'([一二三四五六七八九十])(?=[月日号])')
# 明确编号语境里的孤立单字：「第一部分」「第三章」「第十集」。
# 只认「第 + 单个数字/单位字 + 编号量词」，避免误伤「第一次」「第一时间」
# 「一览表」「统一部署」。单列出来是因为 NUM_RUN / PER_RUN 都要求 >=2 字，
# 这些单字编号原本没有任何通道能处理。
_ORDINAL_NOUN = (
    '部分|单元|單元|阶段|階段|章节|章節|章|节|節|篇|卷|冊|册|条|條|款|'
    '项|項|回|集|期|讲|講|课|課|步|幕|版|类|類|种|種|层|層|级|級|部'
)
ORDINAL_SINGLE = re.compile(
    r'(第)([' + DIGIT_CLS + UNIT_CLS + r'])(?=(?:' + _ORDINAL_NOUN + r'))')  # modified

# 末尾数字前是否紧跟"零"（决定要不要口语补位）
TAIL_PAT = re.compile(r'[' + UNIT_CLS + BIG_CLS + r']([零〇○]?)[' + DIGIT_CLS + r']$')

_NEXT_UNIT = {10: 1, 100: 10, 1000: 100, 10000: 1000, 100000000: 10000000}


def parse(s):
    """把纯中文数字串解析成 int/float，无法解析返回 None。"""
    if not s:
        return None
    s = s.strip()
    if not s:
        return None

    # 小数
    if '点' in s or '點' in s:
        parts = re.split(r'[点點]', s, maxsplit=1)
        ip = parse(parts[0]) if parts[0].strip() else 0
        if ip is None:
            return None
        tail = parts[1]
        if not tail or any(c not in DIGITS for c in tail):
            return None
        frac = ''.join(str(DIGITS[c]) for c in tail)
        return float('%d.%s' % (int(ip), frac))

    if any(c not in ALL_CHARS for c in s):
        return None

    total = 0        # 万/亿以上累计
    section = 0      # 当前"万"以下段
    num = None       # None=没有待结算数字；0=刚读到零。两者必须区分，否则"一百零十"会被当成合法
    last_unit = None
    seen_big = []

    for c in s:
        if c in DIGITS:
            d = DIGITS[c]
            if d == 0:
                # 零只能跟在单位后或串首。前面挂着未结算的数字（如 一万二零三）就是病句
                if num is not None and num != 0:
                    return None
                num = 0
            else:
                # 连续两个非零数字之间没有单位，不是数值读法（那是逐位读法的事）
                if num is not None and num != 0:
                    return None
                num = d
        elif c in UNITS:
            if num == 0:
                return None          # 一百零十：零后面不能直接接单位
            u = UNITS[c]
            n = 1 if num is None else num    # "十五" 的"十"前面没数字，按 1 算
            section += n * u
            num = None
            last_unit = u
        else:
            u = BIG[c]
            if u in seen_big:
                return None          # 一万万
            if seen_big and u > seen_big[-1]:
                return None          # 一万三亿：大单位必须按 亿→万 降序出现
            # “亿万富翁”不是数量：大单位不能作为整串开头（十万/千万已有前导单位）
            if num is None and section == 0:
                return None
            seen_big.append(u)
            section += 0 if num is None else num
            num = None
            if u == 10000:
                total += section * 10000
            else:
                total = (total + section) * 100000000
            section = 0
            last_unit = u

    # 整串一个单位都没有：单个数字字仍可用（小数的整数部分「三」点一四），
    # 多字无单位（一二三）不是数值读法，交给逐位读法
    if last_unit is None:
        if len(s) == 1 and s in DIGITS:
            return DIGITS[s]
        return None

    # 口语省略补位：一百一 / 一千五 / 一万二
    if num and last_unit is not None:
        m = TAIL_PAT.search(s)
        if m and not m.group(1):          # 有"零"占位则不补位
            nxt = _NEXT_UNIT.get(last_unit)
            if nxt:
                section += num * nxt
                num = 0

    return total + section + (num or 0)


def parse_per_digit(s):
    """逐位读法：一九九八 -> 1998。含非数字字返回 None。"""
    if not s or any(c not in DIGITS for c in s):
        return None
    # 全零串（如 〇〇〇）没有意义
    if all(DIGITS[c] == 0 for c in s):
        return None
    return int(''.join(str(DIGITS[c]) for c in s))


def _num_str(q):
    """110.0 -> '110'，1.2 -> '1.2'"""
    if q == int(q):
        return str(int(q))
    return ('%.2f' % q).rstrip('0').rstrip('.')


def _fmt(v, big_unit='plain'):
    """
    格式化数值。
    big_unit='cn' 时，大于一万的整数改用「万/亿」表示，更符合中文习惯：
        1100000 -> 110万      35000000 -> 3500万      120000000 -> 1.2亿
    不能被整除的保持原样：1500 -> 1500（不会变成 0.15万）
    """
    if isinstance(v, float):
        if v.is_integer():
            v = int(v)
        else:
            return ('%g' % v)

    if big_unit == 'cn' and v >= 10000:
        if v >= 100000000 and v % 1000000 == 0:
            return _num_str(v / 100000000.0) + '亿'
        # 只有整万才缩写，避免“十万八千里”变成“10.8万里”。
        if v % 10000 == 0:
            return _num_str(v / 10000.0) + '万'
    return str(int(v))


def _looks_like_repeat(s):
    """
    只挡真正的叠词（三三两两、一二一二、三三），不挡普通编号。
    老写法是 len(set(s))<=2，会把 一二一(121)、一一二一(1121) 这类合法编号一起误杀。
    """
    n = len(s)
    if n < 3:
        return False
    if len(set(s)) == 1:
        return True                                    # AAAA
    if n == 4 and s[0] == s[1] and s[2] == s[3] and s[0] != s[2]:
        return True                                    # AABB：三三两两
    if n % 2 == 0 and s[:n // 2] == s[n // 2:]:
        # 二〇二〇、一九一九是常见年份，不能按叠词挡掉；
        # 一二一二仍保留“歧义编号不转换”的保守行为。
        if n == 4 and (s[1] in '〇零' or (s[0] in '一二' and s[1] in '九〇零')):
            return False
        return True                                    # ABAB：一二一二
    return False


def convert_text(text, unit_read=True, per_digit=True, min_per_len=3,
                 big_unit='plain', ordinal_single=False):
    """
    把文本中的中文数字转成阿拉伯数字。

    big_unit: 'plain' 直接给完整数字（1100000）；'cn' 用万/亿（110万）
    ordinal_single: 把「第 + 单字数字 + 编号量词」也转（第一部分 -> 第1部分），
                    默认关闭，避免把「第一次」改成「第1次」。
    返回 (新文本, 变更列表)
    变更项: {'from','to','kind'}
    """
    changes = []

    if unit_read:
        def _repl_decimal(m):
            s = m.group(0)
            # 小数不走 float：三点一四一五九二六 经 %g 会丢成 3.14159，必须按原串逐位拼
            head, tail = re.split(r'[点點]', s, maxsplit=1)
            if not tail or any(c not in DIGITS for c in tail):
                return s
            ip = parse(head) if head.strip() else None
            if ip is None:
                return s
            out = '%d.%s' % (int(ip), ''.join(str(DIGITS[c]) for c in tail))
            changes.append({'from': s, 'to': out, 'kind': 'decimal'})
            return out

        text = DECIMAL_RUN.sub(_repl_decimal, text)

        def _repl_unit(m):
            s = m.group(0)
            # 必须含权位单位，否则交给逐位读法
            if not any(c in UNITS or c in BIG for c in s):
                return s
            v = parse(s)
            if v is None:
                return s
            out = _fmt(v, big_unit)
            if out == s:
                return s
            changes.append({'from': s, 'to': out, 'kind': 'unit'})
            return out

        text = NUM_RUN.sub(_repl_unit, text)

        def _repl_cal(m):
            s = m.group(1)
            out = str(DIGITS[s])
            changes.append({'from': s, 'to': out, 'kind': 'calendar'})
            return out

        text = CAL_SINGLE.sub(_repl_cal, text)

    if per_digit:
        try:
            min_len = int(min_per_len)
        except (TypeError, ValueError):
            min_len = 3

        def _repl_per(m):
            s = m.group(0)
            if len(s) < min_len:
                return s
            if any(c in UNITS or c in BIG for c in s):
                return s
            # 与单位字紧邻的片段不是独立逐位编号（一五一十、一二三百）
            i, j = m.start(), m.end()
            if ((i > 0 and (text[i - 1] in UNITS or text[i - 1] in BIG))
                    or (j < len(text) and (text[j] in UNITS or text[j] in BIG))):
                return s
            # 只挡真正的叠词，不误伤 121 / 1121 这类编号
            if _looks_like_repeat(s):
                return s
            v = parse_per_digit(s)
            if v is None:
                return s
            # 逐位读法要保留前导零：编号〇〇七 -> 007，不能经 int 变成 7
            out = ''.join(str(DIGITS[c]) for c in s)
            changes.append({'from': s, 'to': out, 'kind': 'per_digit'})
            return out

        text = PER_RUN.sub(_repl_per, text)

    # 放在最后：前面的通道已把「第十一章」变成「第11章」，这里只处理
    # 剩下的单字编号（第 + 一个中文字 + 量词），不会二次命中阿拉伯数字。
    if ordinal_single:  # modified
        def _repl_ordinal(m):  # modified
            s = m.group(2)  # modified
            v = parse(s)  # modified
            if v is None:  # modified
                return m.group(0)  # modified
            out = str(int(v))  # modified
            changes.append({'from': '第' + s, 'to': '第' + out,  # modified
                            'kind': 'ordinal'})  # modified
            return m.group(1) + out  # modified

        text = ORDINAL_SINGLE.sub(_repl_ordinal, text)  # modified

    return text, changes


# ---------- 自检 ----------

_SELFTEST = [
    ('一百一十', 110), ('一百一', 110), ('一百零一', 101),
    ('两百三', 230), ('一千五', 1500), ('一万二', 12000),
    ('一百二十三', 123), ('三千零五', 3005), ('十', 10),
    ('十五', 15), ('二十三', 23), ('两千三百', 2300),
    ('三千五百万', 35000000), ('一亿二千万', 120000000),
    ('壹佰壹拾', 110), ('十一', 11), ('三万五千', 35000),
    # 病句必须拒绝，不能给出貌似确定的错数
    ('一万二零三', None), ('一百零十', None), ('一万万', None),
    ('一万三亿', None), ('一二三', None), ('零五', None),
    ('亿万', None), ('亿', None),
]

# convert_text 的行为用例：(输入, 期望输出)
_TEXTTEST = [
    ('观点一', '观点一'),            # 「点」前无数字，绝不能变成 观0.1
    ('重点二', '重点二'),
    ('卖点三', '卖点三'),
    ('三点一四', '3.14'),
    ('三点一四一五九二六', '3.1415926'),   # 不许被 %g 截断
    ('零点零零零一', '0.0001'),
    ('三三两两', '三三两两'),        # 叠词
    ('一二一二', '一二一二'),
    ('一二一', '121'),              # 普通编号，不能被叠词规则误杀
    ('第一百一十条', '第110条'),
    ('第一次修改', '第一次修改'),      # 孤立单字不动
    ('一九九八年', '1998年'),
    ('二〇二六年', '2026年'),
    ('二〇二〇年', '2020年'),
    ('一九一九年', '1919年'),
    ('二〇二六年一月五日', '2026年1月5日'),
    ('一五一十', '一五一十'),
    ('一二三百', '一二三百'),
    ('亿万富翁', '亿万富翁'),
    ('十万八千里', '108000里'),
]

if __name__ == '__main__':
    bad = 0
    for src, want in _SELFTEST:
        got = parse(src)
        if got != want:
            bad += 1
            print('FAIL %-10s want=%-10s got=%s' % (src, want, got))
    print('--- convert_text ---')
    for src, want in _TEXTTEST:
        got = convert_text(src)[0]
        flag = 'OK ' if got == want else 'FAIL'
        if got != want:
            bad += 1
        print('%s %-14s -> %-14s (want %s)' % (flag, src, got, want))
    print('---')
    print('selftest failures:', bad)
