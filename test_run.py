# -*- coding: utf-8 -*-
"""
端到端回归：规则管线 + 扫描 + 预览 + 执行 + 回滚。
直接运行：python test_run.py
所有文件都在系统临时目录，不写项目 data/，不碰用户文件。
"""
import os
import shutil
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
from core import engine, rules  # noqa: E402

CASES = [
    '第一百一十条 会议纪要2026年10月29日.docx',
    '關於印發《管理辦法》的通知（副本）.pdf',
    '一九九八年度总结报告.txt',
    'ＡＢＣ１２３测试.docx',
    '会议纪要2026年10月29日(1).docx',
    '壹佰壹拾万元预算表.xlsx',
    '測試文档-繁體字.txt',
    '两百三十个样本清单.doc',
    '图片①★说明.png',
    '2026-03-15 关于XX的通知 副本(2).pdf',
    '正常文件名.txt',
    '会议纪要 2026年7月3日 最终版.docx',
]


class EndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix='rename_e2e_')
        cls.sandbox = os.path.join(cls.tmp, 'files')
        os.makedirs(cls.sandbox)
        cls.old_cfg = engine.CONFIG
        cls.old_j = engine.JOURNALS
        engine.CONFIG = os.path.join(cls.tmp, 'config.json')
        engine.JOURNALS = os.path.join(cls.tmp, 'journals')
        engine.save_config({'trusted_roots': [cls.sandbox], 'max_scan': 5000,
                            'journal_keep': 300, 'lower_ext': False})
        for name in CASES:
            with open(os.path.join(cls.sandbox, name), 'w', encoding='utf-8') as f:
                f.write('dummy')

    @classmethod
    def tearDownClass(cls):
        engine.CONFIG = cls.old_cfg
        engine.JOURNALS = cls.old_j
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_preview_execute_rollback(self):
        before = sorted(os.listdir(self.sandbox))
        items, truncated = engine.scan(self.sandbox)
        self.assertFalse(truncated)
        rows = engine.build_preview(items, rules.default_enabled(), rules.default_params())

        print('\n%-46s %-46s %s' % ('原文件名', '新文件名', '状态'))
        print('-' * 96)
        for r in rows:
            print('%-46s %-46s %s' % (r['old_name'][:44], r['new_name'][:44], r['status']))
        picks = [{'src': r['path'], 'new_name': r['new_name'], 'old_name': r['old_name']}
                 for r in rows if r['status'] == 'ok']
        self.assertEqual(len(picks), 11)

        res = engine.execute(self.sandbox, picks, note='selftest')
        self.assertTrue(res['ok'], res)
        self.assertEqual(res['done'], 11, res)
        self.assertFalse(res['failed'])
        self.assertFalse([n for n in os.listdir(self.sandbox) if n.startswith('.~')])
        self.assertEqual(len(os.listdir(self.sandbox)), len(before))

        rb = engine.rollback(res['journal_id'])
        self.assertTrue(rb['ok'], rb)
        self.assertEqual(rb['done'], 11, rb)
        restored = sorted(os.listdir(self.sandbox))
        self.assertEqual(restored, before)
        self.assertFalse([n for n in restored if n.startswith('.~')])


if __name__ == '__main__':
    unittest.main(verbosity=2)
