# -*- coding: utf-8 -*-
"""规则层单元回归：中文数字、日期、清洗、参数边界与空名保护。"""
import sys
import os
import unittest

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
from core import cn_num, rules, text_norm, validate  # noqa: E402


def one(rule_id, text, params=None, ctx=None):
    enabled = {r['id']: r['id'] == rule_id for r in rules.RULES}
    p = rules.default_params()
    p[rule_id].update(params or {})
    return rules.apply_pipeline(text, enabled, p, ctx or {})[0]


class CnNumTest(unittest.TestCase):
    def test_compound_years(self):
        for src, want in [('二〇二〇年', '2020年'), ('一九一九年', '1919年'),
                          ('二〇二六年一月五日', '2026年1月5日')]:
            self.assertEqual(cn_num.convert_text(src)[0], want, src)

    def test_idioms_not_half_transformed(self):
        for text in ('一五一十', '一二三百', '观点一', '重点二'):
            self.assertEqual(cn_num.convert_text(text)[0], text)

    def test_big_unit_idiom(self):
        self.assertEqual(cn_num.convert_text('亿万富翁')[0], '亿万富翁')
        self.assertEqual(cn_num.convert_text('十万八千里')[0], '108000里')
        self.assertIsNone(cn_num.parse('亿万'))

    def test_bad_sentence_rejected(self):
        for text in ('一万二零三', '一百零十', '一万万'):
            self.assertEqual(cn_num.convert_text(text)[0], text)


class DateTest(unittest.TestCase):
    def test_false_month(self):
        self.assertEqual(one('date_front', '会议2026年10个项目', {'style': 'dash'}),
                         '会议2026年10个项目')
        self.assertEqual(one('date_front', '会议2026年度10大新闻', {'style': 'dash'}),
                         '会议2026年度10大新闻')

    def test_day_not_orphan(self):
        self.assertEqual(one('date_front', '会议2026年10月29日9时', {'style': 'dash'},
                             {'year': 2026}),
                         '2026-10-29_会议 9时')

    def test_invalid_date_unchanged(self):
        for text in ('2026年2月29日', '2026年13月5日', '20260229'):
            self.assertEqual(one('date_front', text, {'style': 'dash'}, {'year': 2026}),
                             text)

    def test_earliest_date(self):
        self.assertEqual(one('date_front', '10月29日会议2026年1月5日', {'style': 'dash'},
                             {'year': 2026}),
                         '2026-10-29_会议2026年1月5日')


class CleanBoundaryTest(unittest.TestCase):
    def test_structural_separator_kept(self):
        self.assertEqual(one('clean', '会议记录10:30', {'level': 'normal'}),
                         '会议记录10-30')
        self.assertEqual(one('clean', 'a|b方案', {'level': 'normal'}), 'a-b方案')

    def test_slash_date_not_glued(self):
        enabled = rules.default_enabled()
        p = rules.default_params()
        out = rules.apply_pipeline('会议2026／1／5', enabled, p, {'year': 2026})[0]
        self.assertEqual(out, '2026-01-05_会议')

    def test_default_boundary_matches_after_clean(self):
        enabled = {r['id']: r['id'] in ('clean', 'boundary') for r in rules.RULES}
        p = rules.default_params()
        self.assertEqual(rules.apply_pipeline('会议纪要 - 2026年度 - 草稿', enabled, p, {})[0],
                         '会议纪要')

    def test_empty_results_fall_back(self):
        self.assertEqual(one('boundary', ' - 报告', {'sep': ' - ', 'mode': 'keep_before'}),
                         ' - 报告')
        self.assertEqual(one('template', '报告', {'template': '{date}'}), '报告')
        self.assertEqual(one('insert_del', '报告', {'mode': 'delete', 'count': 999}), '报告')


class ParamTest(unittest.TestCase):
    def test_truncate_uses_keep_not_delete_count(self):
        self.assertEqual(one('insert_del', '关于印发的通知', {'mode': 'truncate'}),
                         '关于印发的通知')
        self.assertEqual(one('insert_del', '关于印发的通知',
                             {'mode': 'truncate', 'keep': 4}), '关于印发')

    def test_regex_literal_backslash(self):
        out = one('regex', 'abc', {'pattern': 'b', 'repl': r'C:\temp'})
        self.assertEqual(out, r'aC:\tempc')       # 字面反斜杠，不是 TAB
        self.assertNotIn('\t', out)
        self.assertFalse(validate.check(out)[0])  # 由最终文件名校验负责拦下非法 \

    def test_huge_digits_is_clamped(self):
        out = one('pad_num', '文件1', {'digits': 10 ** 9})
        self.assertEqual(out, '文件' + '0' * 9 + '1')

    def test_string_min_len_does_not_break_rule(self):
        self.assertEqual(one('cn_num', '一九九八年', {'min_per_len': '3'}), '1998年')

    def test_seq_negative_start_clamped(self):
        out = one('seq', '报告', {'start': -20, 'digits': 2}, {'index': 0, 'total': 1})
        self.assertEqual(out, '00_报告')


class ValidateTest(unittest.TestCase):
    def test_reserved_and_surrogate(self):
        for name in ('CLOCK$.txt', 'COM0.txt', 'CON.txt', 'CONIN$.txt'):
            self.assertFalse(validate.check(name)[0], name)
        ok, reason = validate.check('a\ud800b.txt')
        self.assertFalse(ok)
        self.assertIn('代理', reason)

    def test_text_norm_person_name_not_changed(self):
        # 名字用字不能被本地异体表或 zhconv 静默改写。
        self.assertEqual(text_norm.to_simplified('李喆王甯'), '李喆王甯')


class OrdinalSingleTest(unittest.TestCase):
    """2026-11-11：章节编号里的孤立单字可选转换（默认关闭，不破坏保守性）。"""

    SAMPLE = '新版GMP体系文件训练营第十一章质量控制与产品放行 第一部分'

    def test_default_keeps_conservative_behavior(self):
        out = one('cn_num', self.SAMPLE)
        self.assertIn('第11章', out)
        self.assertIn('第一部分', out)

    def test_enabled_converts_trailing_ordinal(self):
        out = one('cn_num', self.SAMPLE, {'ordinal_single': True})
        self.assertEqual(out, '新版GMP体系文件训练营第11章质量控制与产品放行 第1部分')

    def test_no_false_positives(self):
        for word in ('第一次', '第一时间', '一览表', '统一部署',
                     '第一反应', '一致性', '第一责任人', '万一'):
            self.assertEqual(one('cn_num', word, {'ordinal_single': True}), word)

    def test_unit_char_and_other_nouns(self):
        cases = [('第一部分', '第1部分'), ('第三章', '第3章'),
                 ('第十集', '第10集'), ('第二版', '第2版'),
                 ('第十章', '第10章'), ('第拾篇', '第10篇')]
        for src, want in cases:
            self.assertEqual(one('cn_num', src, {'ordinal_single': True}), want, src)

    def test_malformed_string_does_not_enable(self):
        self.assertEqual(one('cn_num', '第一部分', {'ordinal_single': 'false'}), '第一部分')

    def test_does_not_break_existing_channels(self):
        for src, want in [('第十一章', '第11章'), ('第一百一十章', '第110章'),
                          ('二〇二六年', '2026年'), ('一九九八', '1998')]:
            self.assertEqual(one('cn_num', src, {'ordinal_single': True}), want, src)


class RegexSpaceTest(unittest.TestCase):  # modified
    """查找替换必须能删空格：pattern 里的空格曾被 .strip() 吞掉。"""  # modified

    def test_literal_space_pattern(self):  # modified
        self.assertEqual(one('regex', '新版GMP 第九章 第一部分',  # modified
                             {'pattern': ' ', 'repl': ''}),  # modified
                         '新版GMP第九章第一部分')  # modified

    def test_whitespace_class_pattern(self):  # modified
        self.assertEqual(one('regex', 'A  B\tC',  # modified
                             {'pattern': r'\s+', 'repl': ''}), 'ABC')  # modified

    def test_multiple_spaces_as_pattern(self):  # modified
        self.assertEqual(one('regex', '甲  乙 丙',  # modified
                             {'pattern': '  ', 'repl': '-'}), '甲-乙 丙')  # modified

    def test_empty_pattern_still_disabled(self):  # modified
        self.assertEqual(one('regex', 'A B',  # modified
                             {'pattern': '', 'repl': 'X'}), 'A B')  # modified


if __name__ == '__main__':
    unittest.main(verbosity=2)
