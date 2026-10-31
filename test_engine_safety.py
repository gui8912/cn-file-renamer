# -*- coding: utf-8 -*-
"""改名/回滚安全回归。只操作项目内的 _test_safety 沙箱。"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
from core import engine  # noqa: E402

class EngineSafetyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ROOT = tempfile.mkdtemp(prefix='rename_safety_')
        cls.old_config_path = engine.CONFIG
        cls.old_journals = engine.JOURNALS
        # 测试不读写真实 data/config.json，Ctrl-C 中断也不会污染用户白名单。
        engine.CONFIG = os.path.join(cls.ROOT, 'config.json')
        engine.save_config({'trusted_roots': [cls.ROOT], 'max_scan': 5000,
                            'journal_keep': 300, 'lower_ext': False})

    @classmethod
    def tearDownClass(cls):
        engine.CONFIG = cls.old_config_path
        engine.JOURNALS = cls.old_journals
        shutil.rmtree(cls.ROOT, ignore_errors=True)

    def setUp(self):
        # 短随机目录避免测试方法名把完整路径顶到 Windows MAX_PATH。
        self.dir = tempfile.mkdtemp(prefix='t_', dir=self.ROOT)
        engine.JOURNALS = os.path.join(self.dir, 'journals')

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def make(self, name, text=None):
        p = os.path.join(self.dir, name)
        with open(p, 'w', encoding='utf-8') as f:
            f.write(name if text is None else text)
        return p

    def read(self, name):
        with open(os.path.join(self.dir, name), encoding='utf-8') as f:
            return f.read()

    def run_rename(self, mapping):
        pairs = [{'src': os.path.join(self.dir, old), 'new_name': new}
                 for old, new in mapping]
        res = engine.execute(self.dir, pairs, note='safety-test')
        self.assertTrue(res['ok'], res)
        self.assertEqual(res['done'], len(mapping), res)
        return res

    def test_swap_rename_and_rollback(self):
        self.make('A.txt', 'A')
        self.make('B.txt', 'B')
        res = self.run_rename([('A.txt', 'B.txt'), ('B.txt', 'A.txt')])
        self.assertEqual(self.read('A.txt'), 'B')
        rb = engine.rollback(res['journal_id'])
        self.assertTrue(rb['ok'], rb)
        self.assertEqual(rb['done'], 2)
        self.assertEqual(self.read('A.txt'), 'A')
        self.assertEqual(self.read('B.txt'), 'B')

    def test_three_cycle_rename_and_rollback(self):
        for n in ('A.txt', 'B.txt', 'C.txt'):
            self.make(n)
        res = self.run_rename([('A.txt', 'B.txt'), ('B.txt', 'C.txt'), ('C.txt', 'A.txt')])
        rb = engine.rollback(res['journal_id'])
        self.assertTrue(rb['ok'], rb)
        self.assertEqual(rb['done'], 3)
        for n in ('A.txt', 'B.txt', 'C.txt'):
            self.assertEqual(self.read(n), n)

    def test_rollback_does_not_overwrite_new_file_and_can_retry(self):
        src = self.make('A.txt', 'old')
        res = self.run_rename([('A.txt', 'B.txt')])
        self.make('A.txt', 'new-file-must-survive')
        rb = engine.rollback(res['journal_id'])
        self.assertFalse(rb['ok'])
        self.assertEqual(len(rb['skipped']), 1)
        self.assertEqual(self.read('A.txt'), 'new-file-must-survive')
        self.assertTrue(os.path.exists(os.path.join(self.dir, 'B.txt')))
        # 用户移走占用文件后，可再次回滚同一日志。
        os.replace(src, os.path.join(self.dir, 'keep.txt'))
        rb2 = engine.rollback(res['journal_id'])
        self.assertTrue(rb2['ok'], rb2)
        self.assertEqual(self.read('A.txt'), 'old')
        self.assertEqual(self.read('keep.txt'),
                         'new-file-must-survive')

    def test_partial_rollback_tracks_each_move(self):
        self.make('A.txt', 'A')
        self.make('B.txt', 'B')
        res = self.run_rename([('A.txt', 'X.txt'), ('B.txt', 'Y.txt')])
        # 暂时移走，保留原文件身份；模拟“文件稍后回到原位”，而不是新建一个同名文件。
        held = os.path.join(self.dir, 'held')
        os.replace(os.path.join(self.dir, 'Y.txt'), held)
        rb = engine.rollback(res['journal_id'])
        self.assertFalse(rb['ok'])
        self.assertEqual(rb['done'], 1)
        self.assertTrue(os.path.exists(os.path.join(self.dir, 'A.txt')))
        self.assertFalse(engine.get_journal(res['journal_id'])['rolled_back'])
        # 原文件回到改名后的位置后，第二次只处理剩余项。
        os.replace(held, os.path.join(self.dir, 'Y.txt'))
        rb2 = engine.rollback(res['journal_id'])
        self.assertTrue(rb2['ok'], rb2)
        self.assertEqual(rb2['done'], 1)
        self.assertTrue(engine.get_journal(res['journal_id'])['rolled_back'])
        self.assertEqual(self.read('B.txt'), 'B')

    def test_crash_after_staging_can_recover(self):
        self.make('A.txt', 'A')
        self.make('B.txt', 'B')
        real_rename = engine.os.rename
        calls = {'n': 0}

        def crash_after_first(src, dst):
            real_rename(src, dst)
            calls['n'] += 1
            if calls['n'] == 1:
                raise RuntimeError('simulated power failure')

        engine.os.rename = crash_after_first
        jid = None
        try:
            engine.execute(self.dir, [
                {'src': os.path.join(self.dir, 'A.txt'), 'new_name': 'X.txt'},
                {'src': os.path.join(self.dir, 'B.txt'), 'new_name': 'Y.txt'},
            ])
        except RuntimeError:
            files = [f for f in os.listdir(engine.JOURNALS) if f.endswith('.json')]
            self.assertEqual(len(files), 1)
            jid = files[0][:-5]
        finally:
            engine.os.rename = real_rename
        self.assertIsNotNone(jid)
        self.assertTrue(engine.get_journal(jid)['staged'])
        rb = engine.rollback(jid)
        self.assertTrue(rb['ok'], rb)
        self.assertEqual(self.read('A.txt'), 'A')
        self.assertEqual(self.read('B.txt'), 'B')
        self.assertFalse([n for n in os.listdir(self.dir) if n.startswith('.~')])

    def test_long_name_uses_short_temp_name(self):
        # 当前 Windows 未启用长路径策略，沙箱全路径需控制在 260 字以内；
        # 另用 _temp_path 的长度断言覆盖“临时名不能拼原名”的回归。
        name = '长' * 150 + '.txt'
        self.make(name, 'content')
        res = self.run_rename([(name, '短.txt')])
        self.assertTrue(os.path.exists(os.path.join(self.dir, '短.txt')))
        self.assertLess(len(os.path.basename(engine._temp_path(name, 'ren'))), 40)
        rb = engine.rollback(res['journal_id'])
        self.assertTrue(rb['ok'], rb)
        self.assertTrue(os.path.exists(os.path.join(self.dir, name)))

    def test_auto_dedupe_protects_other_planned_target(self):
        rows = [
            {'dir': self.dir, 'old_name': 'a.txt', 'new_name': 'X(1).txt', 'status': 'ok'},
            {'dir': self.dir, 'old_name': 'b.txt', 'new_name': 'X.txt', 'status': 'conflict'},
        ]
        engine.auto_resolve_conflicts(rows)
        self.assertEqual(rows[1]['new_name'], 'X(2).txt')
        self.assertEqual(rows[1]['status'], 'ok')

    def test_parent_and_child_cannot_be_renamed_together(self):
        parent = os.path.join(self.dir, 'ＡＢＣ')
        os.makedirs(parent)
        child = os.path.join(parent, 'a.txt')
        with open(child, 'w', encoding='utf-8') as f:
            f.write('A')
        items = [
            {'name': 'ＡＢＣ', 'dir': self.dir, 'path': parent,
             'is_dir': True, 'size': 0, 'mtime': 0},
            {'name': 'a.txt', 'dir': parent, 'path': child,
             'is_dir': False, 'size': 1, 'mtime': 0},
        ]
        rows = engine.build_preview(items)
        by_name = {r['old_name']: r for r in rows}
        self.assertEqual(by_name['ＡＢＣ']['status'], 'conflict')
        self.assertIn('分两批', by_name['ＡＢＣ']['reason'])
        with self.assertRaises(ValueError):
            engine.execute(self.dir, [
                {'src': child, 'new_name': 'b.txt'},
                {'src': parent, 'new_name': 'XYZ'},
            ])
        self.assertTrue(os.path.exists(child))
        self.assertFalse([n for n in os.listdir(self.dir) if n.startswith('.~')])

    def test_rollback_crash_is_recoverable(self):
        self.make('A.txt', 'A')
        self.make('B.txt', 'B')
        res = self.run_rename([('A.txt', 'X.txt'), ('B.txt', 'Y.txt')])
        real_rename = engine.os.rename
        calls = {'n': 0}

        def crash_after_first(src, dst):
            real_rename(src, dst)
            calls['n'] += 1
            if calls['n'] == 1:
                raise RuntimeError('simulated rollback crash')

        engine.os.rename = crash_after_first
        try:
            engine.rollback(res['journal_id'])
        except RuntimeError:
            pass
        finally:
            engine.os.rename = real_rename
        j = engine.get_journal(res['journal_id'])
        self.assertTrue(j.get('rb_staged'))
        rb = engine.rollback(res['journal_id'])
        self.assertTrue(rb['ok'], rb)
        self.assertEqual(self.read('A.txt'), 'A')
        self.assertEqual(self.read('B.txt'), 'B')
        self.assertFalse([n for n in os.listdir(self.dir) if n.startswith('.~')])

    def test_phase2_failure_leaves_no_unreported_orphan(self):
        self.make('A.txt', 'A')
        real_rename = engine.os.rename
        calls = {'n': 0}

        def fail_move_and_restore(src, dst):
            calls['n'] += 1
            if calls['n'] >= 2:
                raise OSError('simulated move failure')
            return real_rename(src, dst)

        engine.os.rename = fail_move_and_restore
        try:
            res = engine.execute(self.dir, [
                {'src': os.path.join(self.dir, 'A.txt'), 'new_name': 'B.txt'},
            ])
        finally:
            engine.os.rename = real_rename
        self.assertFalse(res['ok'])
        self.assertTrue([n for n in os.listdir(self.dir) if n.startswith('.~ren~')])
        j = engine.get_journal(res['journal_id'])
        self.assertEqual(j['status'], 'partial_orphan')
        self.assertTrue(j['staged'])
        rb = engine.rollback(res['journal_id'])
        self.assertTrue(rb['ok'], rb)
        self.assertEqual(self.read('A.txt'), 'A')
        self.assertFalse([n for n in os.listdir(self.dir) if n.startswith('.~')])

    def test_rollback_refuses_replaced_file(self):
        self.make('A.txt', 'A')
        res = self.run_rename([('A.txt', 'X.txt')])
        os.remove(os.path.join(self.dir, 'X.txt'))
        self.make('X.txt', 'USER-NEW')
        rb = engine.rollback(res['journal_id'])
        self.assertFalse(rb['ok'])
        self.assertEqual(len(rb['skipped']), 1)
        self.assertEqual(self.read('X.txt'), 'USER-NEW')
        self.assertFalse(os.path.exists(os.path.join(self.dir, 'A.txt')))

    def test_unc_path_is_blocked(self):
        self.assertTrue(engine.is_path_blocked(r'\\localhost\C$\Windows'))
        ok, reason = engine.check_root_allowed(r'\\localhost\C$\Windows')
        self.assertFalse(ok)
        self.assertIn('网络共享', reason)

    @unittest.skipUnless(os.name == 'nt', 'junction 仅 Windows')
    def test_junction_alias_cannot_bypass_parent_check(self):
        real = os.path.join(self.dir, 'real')
        sub = os.path.join(real, 'sub')
        os.makedirs(sub)
        child = os.path.join(sub, 't.txt')
        with open(child, 'w', encoding='utf-8') as f:
            f.write('T')
        alias = os.path.join(self.dir, 'link')
        p = subprocess.run(['cmd', '/c', 'mklink', '/J', alias, real],
                           capture_output=True, text=True,
                           encoding='mbcs', errors='ignore')
        if p.returncode != 0:
            self.skipTest('当前卷不支持 junction：' + p.stderr)
        with self.assertRaises(ValueError):
            engine.execute(self.dir, [
                {'src': alias, 'new_name': 'link2'},
                {'src': child, 'new_name': 't2.txt'},
            ])
        self.assertTrue(os.path.exists(child))
        os.rmdir(alias)

    def test_reading_journals_during_execute_is_safe(self):
        mapping = []
        for i in range(30):
            name = 'f%02d.txt' % i
            self.make(name, name)
            mapping.append((name, 'n%02d.txt' % i))
        errors = []
        done = {'v': False}

        def reader():
            while not done['v']:
                try:
                    engine.list_journals()
                    engine.list_journals()
                except Exception as e:
                    errors.append(repr(e))

        t = threading.Thread(target=reader)
        t.start()
        try:
            res = self.run_rename(mapping)
        finally:
            done['v'] = True
            t.join()
        self.assertFalse(errors, errors[:3])
        self.assertTrue(res['ok'])

    def test_concurrent_rollback_is_serialized(self):
        self.make('A.txt', 'A')
        res = self.run_rename([('A.txt', 'X.txt')])
        results = []
        errors = []

        def work():
            try:
                results.append(engine.rollback(res['journal_id']))
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=work) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(1 for r in results if r.get('ok')), 1, (results, errors))
        self.assertEqual(len(errors), 2)
        self.assertEqual(self.read('A.txt'), 'A')
        self.assertFalse(os.path.exists(os.path.join(self.dir, 'X.txt')))

    def test_scan_include_exclude_filters(self):
        self.make('年报.pdf')
        self.make('年报.docx')
        self.make('~$年报.docx')
        self.make('会议2026年总结.txt')
        sub = os.path.join(self.dir, '子目录')
        os.makedirs(sub)
        with open(os.path.join(sub, '附件.PDF'), 'w') as f:
            f.write('x')
        items, truncated = engine.scan(self.dir, recursive=True,
                                       include='pdf;docx', exclude='~$*')
        names = sorted(i['name'] for i in items)
        self.assertEqual(names, ['年报.docx', '年报.pdf', '附件.PDF'])
        self.assertFalse(truncated)
        items2, _ = engine.scan(self.dir, include='2026')
        self.assertEqual([i['name'] for i in items2], ['会议2026年总结.txt'])
        # 目录不受文件类型筛选影响，勾“包含文件夹”时仍可出现。
        items3, _ = engine.scan(self.dir, include_dirs=True, include='pdf', exclude='年报')
        self.assertIn('子目录', [i['name'] for i in items3])
        self.assertNotIn('年报.pdf', [i['name'] for i in items3])

    def test_duplicate_source_is_skipped(self):
        src = self.make('A.txt', 'A')
        res = engine.execute(self.dir, [
            {'src': src, 'new_name': 'B.txt'},
            {'src': src, 'new_name': 'C.txt'},
        ])
        self.assertEqual(res['done'], 1, res)
        self.assertEqual(len(res['skipped']), 1, res)
        self.assertTrue(os.path.exists(os.path.join(self.dir, 'B.txt')))
        self.assertFalse(os.path.exists(os.path.join(self.dir, 'C.txt')))


if __name__ == '__main__':
    unittest.main(verbosity=2)
