# -*- coding: utf-8 -*-
"""2026-11-05 源码审计回归：本轮修复项的可复现用例。

本文件为本轮【新增】，整体视为 # modified；各测试方法上的 # modified
标注该方法对应的修复编号，便于以后按图索骥。
只依赖 Python 标准库，与项目"零 pip 依赖"的约束一致。
"""

import os  # modified
import shutil  # modified
import sys  # modified
import tempfile  # modified
import threading  # modified
import unicodedata  # modified
import unittest  # modified
from unittest import mock  # modified

BASE = os.path.dirname(os.path.abspath(__file__))  # modified
if BASE not in sys.path:  # modified
    sys.path.insert(0, BASE)  # modified

from core import engine, llm, rules, shell_ext, text_norm  # modified


class TextNormRegression(unittest.TestCase):  # modified
    """F-01：NFC 归一化 + 私用区占位符不再误还原。"""

    def test_nfd_combining_mark_is_not_dropped(self):  # modified (F-01)
        # PowerRename 的 SanitizeAndNormalize 是先 NFC 再清洗。
        # 原来 cafe\u0301报告.pdf 会被清洗成 cafe报告.pdf（é 变 e）。
        e, p = rules.default_enabled(), rules.default_params()  # modified
        out = rules.apply_pipeline('cafe\u0301报告.pdf', e, p,  # modified
                                   {'index': 0, 'total': 1})[0]  # modified
        self.assertIn('caf\u00e9', out)  # modified

    def test_bmp_private_use_char_is_preserved(self):  # modified (F-01)
        # 原来 U+E000 会被占位符反向还原成「喆」。
        s = '\ue000报告.txt'  # modified
        self.assertEqual(text_norm.to_simplified(s), s)  # modified
        self.assertEqual(  # modified
            text_norm.clean_symbols(text_norm.to_simplified(s), 'off'), s)  # modified

    def test_supplementary_private_use_char_is_preserved(self):  # modified (F-01)
        s = chr(0xF0000) + '报告.txt'  # modified
        self.assertEqual(text_norm.to_simplified(s), s)  # modified

    def test_name_chars_are_still_protected(self):  # modified (F-01 回归)
        self.assertEqual(text_norm.to_simplified('李喆王甯'), '李喆王甯')  # modified
        self.assertEqual(text_norm.to_simplified('關於印發'), '关于印发')  # modified

    def test_lone_surrogate_does_not_raise(self):  # modified (F-01 回归)
        self.assertEqual(text_norm.to_simplified('a\ud800b'), 'a\ud800b')  # modified


class ConfigWriteRegression(unittest.TestCase):  # modified
    """F-05：save_config 唯一临时名 + fsync + 原子替换。"""

    def setUp(self):  # modified
        self.tmp = tempfile.TemporaryDirectory()  # modified
        self.orig = (engine.DATA, engine.CONFIG)  # modified
        engine.DATA = self.tmp.name  # modified
        engine.CONFIG = os.path.join(self.tmp.name, 'config.json')  # modified

    def tearDown(self):  # modified
        engine.DATA, engine.CONFIG = self.orig  # modified
        self.tmp.cleanup()  # modified

    @staticmethod
    def _cfg(i):  # modified
        return {'trusted_roots': ['D:\\x%d' % i], 'max_scan': 5000,  # modified
                'journal_keep': 300, 'lower_ext': False}  # modified

    def test_parallel_plain_saves_never_raise(self):  # modified (F-05)
        # 不用 config_lock 时，Windows 上并发 os.replace 到同一目标会抛
        # PermissionError(13)；save_config 的退避重试必须自己兜住。
        errors = []  # modified

        def writer(i):  # modified
            try:  # modified
                engine.save_config(self._cfg(i))  # modified
            except Exception as exc:  # modified
                errors.append(exc)  # modified

        threads = [threading.Thread(target=writer, args=(i,))  # modified
                   for i in range(12)]  # modified
        for t in threads:  # modified
            t.start()  # modified
        for t in threads:  # modified
            t.join()  # modified
        self.assertEqual(errors, [])  # modified

    def test_config_lock_prevents_lost_update(self):  # modified (F-05)
        errors = []  # modified

        def writer(i):  # modified
            try:  # modified
                with engine.config_lock():  # modified
                    cfg = engine.load_config()  # modified
                    cfg['trusted_roots'].append('D:\\x%d' % i)  # modified
                    engine.save_config(cfg)  # modified
            except Exception as exc:  # modified
                errors.append(exc)  # modified

        threads = [threading.Thread(target=writer, args=(i,))  # modified
                   for i in range(12)]  # modified
        for t in threads:  # modified
            t.start()  # modified
        for t in threads:  # modified
            t.join()  # modified
        self.assertEqual(errors, [])  # modified
        cfg = engine.load_config()  # modified
        self.assertEqual(len(cfg['trusted_roots']), 12)  # modified
        self.assertEqual(set(cfg['trusted_roots']),  # modified
                         {'D:\\x%d' % i for i in range(12)})  # modified
        leftovers = [n for n in os.listdir(self.tmp.name)  # modified
                     if '.tmp' in n]  # modified
        self.assertEqual(leftovers, [])  # modified

    def test_no_fixed_tmp_name_anymore(self):  # modified (F-05)
        engine.save_config(self._cfg(0))  # modified
        self.assertFalse(os.path.exists(engine.CONFIG + '.tmp'))  # modified
        self.assertTrue(os.path.exists(engine.CONFIG))  # modified


class NfcOnlyRenameRegression(unittest.TestCase):  # modified
    """R-01（复审补修）：只差 NFC 编码的改名必须在预览里说明原因。

    韩文 / 越南语 / 带声调拼音常以 NFD 存储，NFC 后字符串与字节都变，
    但资源管理器里看起来一模一样；不提示的话用户会以为"改了却没变化"。
    """

    def test_nfc_only_change_is_explained(self):  # modified (R-01)
        d = tempfile.mkdtemp()  # modified
        try:  # modified
            name = unicodedata.normalize('NFD', '한국어보고서.txt')  # modified
            p = os.path.join(d, name)  # modified
            with open(p, 'w', encoding='utf-8') as fh:  # modified
                fh.write('x')  # modified
            items = [{'name': name, 'dir': d, 'path': p,  # modified
                      'is_dir': False, 'size': 1, 'mtime': 0}]  # modified
            rows = engine.build_preview(items, None, None)  # modified
            self.assertEqual(rows[0]['status'], 'ok')  # modified
            self.assertIn('Unicode 归一化', rows[0]['reason'])  # modified
            self.assertNotEqual(rows[0]['old_name'], rows[0]['new_name'])  # modified
        finally:  # modified
            shutil.rmtree(d, ignore_errors=True)  # modified

    def test_plain_name_has_no_extra_reason(self):  # modified (R-01 回归)
        d = tempfile.mkdtemp()  # modified
        try:  # modified
            p = os.path.join(d, '普通文件.txt')  # modified
            with open(p, 'w', encoding='utf-8') as fh:  # modified
                fh.write('x')  # modified
            items = [{'name': '普通文件.txt', 'dir': d, 'path': p,  # modified
                      'is_dir': False, 'size': 1, 'mtime': 0}]  # modified
            rows = engine.build_preview(items, None, None)  # modified
            self.assertNotIn('Unicode', rows[0]['reason'])  # modified
        finally:  # modified
            shutil.rmtree(d, ignore_errors=True)  # modified


@unittest.skipUnless(os.name == 'nt', '右键菜单注册表用例仅适用于 Windows')  # modified
class ShellStatusRegression(unittest.TestCase):  # modified
    """F-06：部分安装不再误报「已安装」。"""

    def test_missing_one_key_is_partial_and_needs_reinstall(self):  # modified (F-06)
        class FakeKey:  # modified
            def __enter__(self):  # modified
                return self  # modified

            def __exit__(self, *a):  # modified
                return False  # modified

        def fake_open(_root, path, *_a):  # modified
            # 只让「右键文件夹」这个键缺失，另外两个键正常存在。
            if path == shell_ext.KEY_DIR + r'\command':  # modified
                raise OSError('missing')  # modified
            return FakeKey()  # modified

        # modified: 模拟注册表只装了两个键（缺 KEY_DIR/command）
        with mock.patch.object(shell_ext, 'is_windows', return_value=True), \
             mock.patch.object(shell_ext.winreg, 'OpenKey', side_effect=fake_open), \
             mock.patch.object(shell_ext.winreg, 'QueryValueEx',
                               return_value=('', 0)):
            s = shell_ext.status()  # modified
        self.assertTrue(s['installed'])  # modified
        self.assertFalse(s['installed_all'])  # modified
        self.assertTrue(s['partial'])  # modified
        self.assertTrue(s['needs_reinstall'])  # modified

    def test_clean_install_reports_all_fields(self):  # modified (F-06 回归)
        class FakeKey:  # modified
            def __enter__(self):  # modified
                return self  # modified

            def __exit__(self, *a):  # modified
                return False  # modified

        cmd = '"pythonw.exe" "%s" "%%V"' % shell_ext.LAUNCHER  # modified
        # modified: 三个键齐全且指向本目录，应判定无需重装
        with mock.patch.object(shell_ext, 'is_windows', return_value=True), \
             mock.patch.object(shell_ext.winreg, 'OpenKey',
                               return_value=FakeKey()), \
             mock.patch.object(shell_ext.winreg, 'QueryValueEx',
                               return_value=(cmd, 0)):
            s = shell_ext.status()  # modified
        self.assertTrue(s['installed'])  # modified
        self.assertTrue(s['installed_all'])  # modified
        self.assertFalse(s['partial'])  # modified
        self.assertFalse(s['needs_reinstall'])  # modified


class ExecuteInputRegression(unittest.TestCase):  # modified
    """F-12：畸形执行条目跳过，不再抛 KeyError。"""

    def test_malformed_pairs_are_skipped_with_reason(self):  # modified (F-12)
        res = engine.execute('', [  # modified
            {'new_name': 'x.txt'}, None, 123, {'src': ''},  # modified
        ])  # modified
        self.assertFalse(res['ok'])  # modified
        self.assertEqual(res['done'], 0)  # modified
        self.assertEqual(len(res['skipped']), 4)  # modified
        for item in res['skipped']:  # modified
            self.assertIn('src', item)  # modified
            self.assertTrue(item['reason'])  # modified


class LlmResponseRegression(unittest.TestCase):  # modified
    """F-09：模型响应体有大小上限。"""

    def test_oversize_response_is_rejected(self):  # modified (F-09)
        class R:  # modified
            def __enter__(self):  # modified
                return self  # modified

            def __exit__(self, *a):  # modified
                return False  # modified

            def read(self, _n=-1):  # modified
                return b'x' * (2 * 1024 * 1024 + 2)  # modified

        cfg = {'base_url': 'http://127.0.0.1:1/v1', 'model': 'x',  # modified
               'api_key': '', 'timeout': 1, 'temperature': 0.2}  # modified
        with mock.patch('urllib.request.urlopen', return_value=R()):  # modified
            with self.assertRaisesRegex(ValueError, '响应体过大'):  # modified
                llm._chat([{'role': 'user', 'content': 'hi'}], cfg)  # modified


class LauncherMixedSelectionRegression(unittest.TestCase):  # modified
    """F-08：文件 + 目录混合多选时文件不再被静默丢弃。"""

    def test_mixed_selection_keeps_both_in_url(self):  # modified (F-08)
        import launcher  # modified
        with tempfile.TemporaryDirectory() as d:  # modified
            f = os.path.join(d, 'a.txt')  # modified
            with open(f, 'w', encoding='utf-8') as fh:  # modified
                fh.write('x')  # modified
            sub = os.path.join(d, 'sub')  # modified
            os.mkdir(sub)  # modified
            # modified: 不真开浏览器、不真写授权，只捕获生成的 URL
            with mock.patch.object(sys, 'argv', ['launcher.py', sub, f]), \
                 mock.patch.object(launcher, 'find_server', return_value=8770), \
                 mock.patch.object(launcher, 'authorize'), \
                 mock.patch.object(launcher, 'log') as mlog, \
                 mock.patch.dict(os.environ,
                                 {'RENAME_LAUNCHER_NO_OPEN': '1'}):
                self.assertEqual(launcher.main(), 0)  # modified
            urls = [str(c.args[0]) for c in mlog.call_args_list  # modified
                    if '目标地址' in str(c.args[0])]  # modified
            self.assertEqual(len(urls), 1)  # modified
            self.assertIn('path=', urls[0])  # modified
            self.assertIn('a.txt', urls[0])  # modified
            self.assertIn('sub', urls[0])  # modified
            self.assertNotIn('auto=1', urls[0])  # modified


if __name__ == '__main__':  # modified
    unittest.main(verbosity=2)  # modified
