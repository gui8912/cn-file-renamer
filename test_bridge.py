# -*- coding: utf-8 -*-
"""外部路径桥 / 原生目录选择 / 反选与列头排序相关服务端逻辑的回归。

不弹任何 GUI 窗口：所有 Win32 对话框与 HTTP 通信都用 mock 挡掉，
因此可在 CI 和笔记本上直接运行。
"""

import json  # modified
import os  # modified
import shutil  # modified
import sys  # modified
import tempfile  # modified
import threading  # modified
import unittest  # modified
from unittest import mock  # modified

BASE = os.path.dirname(os.path.abspath(__file__))  # modified
if BASE not in sys.path:  # modified
    sys.path.insert(0, BASE)  # modified

import app as app_mod  # modified
import folder_bridge as bridge  # modified
import launcher  # modified


class FakeResponse:  # modified
    """urlopen 的极简替身，返回 /api/ping 的 JSON。"""  # modified

    def __init__(self, payload):  # modified
        self._data = json.dumps(payload).encode('utf-8')  # modified

    def __enter__(self):  # modified
        return self  # modified

    def __exit__(self, *args):  # modified
        return False  # modified

    def read(self):  # modified
        return self._data  # modified


class SessionMixin:  # modified
    """每个用例都把全局 _SESSION 还原，避免测试间互相污染。"""  # modified

    def setUp(self):  # modified
        self._saved = dict(app_mod._SESSION)  # modified
        self.tmp = tempfile.mkdtemp(prefix='bridge_test_')  # modified

    def tearDown(self):  # modified
        app_mod._SESSION.clear()  # modified
        app_mod._SESSION.update(self._saved)  # modified
        shutil.rmtree(self.tmp, ignore_errors=True)  # modified

    def make_file(self, name='报告.txt'):  # modified
        p = os.path.join(self.tmp, name)  # modified
        with open(p, 'w', encoding='utf-8') as f:  # modified
            f.write('x')  # modified
        return p  # modified


class BridgePathTest(unittest.TestCase):  # modified
    """插件侧：路径规范化与端口发现。"""  # modified

    def test_normalize_paths_dedup_and_drop_missing(self):  # modified
        p = os.path.abspath(__file__)  # modified
        out = bridge.normalize_paths(  # modified
            ['', '   ', p, p, os.path.join(BASE, '__no_such_file__')])  # modified
        self.assertEqual(out, [p])  # modified

    def test_find_server_accepts_symlink_style_base(self):  # modified
        # 服务台起的服务 base 是符号链接路径，字符串与 BASE 不等但 realpath 相等。
        ping = {'ok': True, 'app': 'rename-tool-cn',  # modified
                'base': r'D:\Tools\服务台\apps\rename-cn'}  # modified
        with mock.patch.object(bridge, '_port_open',  # modified
                               side_effect=lambda p: p == 8770), \
             mock.patch.object(bridge.urllib.request, 'urlopen',  # modified
                               return_value=FakeResponse(ping)):  # modified
            port, got = bridge.find_server()  # modified
        self.assertEqual(port, 8770)  # modified
        self.assertEqual(got['app'], 'rename-tool-cn')  # modified

    def test_launcher_find_server_uses_realpath(self):  # modified
        ping = {'ok': True, 'app': 'rename-tool-cn',  # modified
                'base': r'D:\Tools\服务台\apps\rename-cn'}  # modified
        with mock.patch.object(launcher, '_port_open',  # modified
                               side_effect=lambda p: p == 8770), \
             mock.patch.object(launcher.urllib.request, 'urlopen',  # modified
                               return_value=FakeResponse(ping)):  # modified
            self.assertEqual(launcher.find_server(), 8770)  # modified

    def test_push_paths_rejects_legacy_server_clearly(self):  # modified
        # 旧服务没有 bridge_rev/sid，硬 POST 会被会话校验拒掉并给出误导性报错，
        # 必须在发请求之前就明确告诉用户「重启服务」。
        with mock.patch.object(bridge, 'find_server',  # modified
                               return_value=(8770, {'ok': True,  # modified
                                                    'app': 'rename-tool-cn'})), \
             mock.patch.object(bridge, '_post',  # modified
                               side_effect=AssertionError('不应向旧服务发请求')):  # modified
            ok, msg = bridge.push_paths([os.path.abspath(__file__)])  # modified
        self.assertFalse(ok)  # modified
        self.assertIn('重启', msg)  # modified

    def test_push_paths_authorizes_before_add_and_marks_bridge(self):  # modified
        p = os.path.abspath(__file__)  # modified
        calls = []  # modified

        def fake_post(port, endpoint, body):  # modified
            calls.append((endpoint, body))  # modified
            if endpoint == '/api/roots/add':  # modified
                return {'ok': True, 'trusted_roots': []}  # modified
            return {'ok': True, 'added': 1, 'errors': [],  # modified
                    'root': BASE, 'sid': 's1'}  # modified

        with mock.patch.object(bridge, 'find_server',  # modified
                               return_value=(8770, {'sid': 's1',  # modified
                                                    'bridge_rev': 0})), \
             mock.patch.object(bridge, '_post', side_effect=fake_post):  # modified
            ok, msg = bridge.push_paths([p])  # modified
        self.assertTrue(ok, msg)  # modified
        self.assertEqual([e for e, _ in calls],  # modified
                         ['/api/roots/add', '/api/add-paths'])  # modified
        body = calls[-1][1]  # modified
        self.assertTrue(body['bridge'])  # modified
        self.assertEqual(body['sid'], 's1')  # modified
        self.assertEqual(body['paths'], [p])  # modified


class PickFolderApiTest(SessionMixin, unittest.TestCase):  # modified
    """服务端：原生对话框返回路径后一步完成授权 + 扫描。"""  # modified

    def _run_pick(self, payload):  # modified
        with mock.patch.object(app_mod.Api, 'add_root',  # modified
                               return_value={'ok': True,  # modified
                                             'trusted_roots': [self.tmp]}), \
             mock.patch.object(app_mod.subprocess, 'run',  # modified
                               return_value=mock.Mock(stdout=self.tmp + '\n')) as mrun:  # modified
            res = app_mod.Api.pick_folder(payload)  # modified
        return res, mrun  # modified

    def test_pick_calls_native_bridge_and_fills_session(self):  # modified
        self.make_file('新版GMP培训 第一部分.mp4')  # modified
        r, mrun = self._run_pick({})  # modified
        cmd = mrun.call_args[0][0]  # modified
        self.assertTrue(any('--pick' in str(x) for x in cmd),  # modified
                        '必须调用外部插件的 --pick，而不是 tkinter')  # modified
        self.assertTrue(r['ok'])  # modified
        self.assertEqual(r['count'], 1)  # modified
        self.assertTrue(r['sid'])  # modified
        self.assertEqual(app_mod._SESSION['root'], os.path.abspath(self.tmp))  # modified
        self.assertEqual(len(app_mod._SESSION['items']), 1)  # modified

    def test_pick_auth_only_does_not_replace_current_list(self):  # modified
        sentinel = [{'name': 'keep.txt'}]  # modified
        app_mod._SESSION['items'] = sentinel  # modified
        r, _ = self._run_pick({'auth_only': True})  # modified
        self.assertTrue(r.get('auth_only'))  # modified
        self.assertEqual(app_mod._SESSION['items'], sentinel,  # modified
                         '授权弹窗里的浏览不能顺手换掉用户正在处理的清单')  # modified

    def test_pick_reports_cancelled_dialog(self):  # modified
        with mock.patch.object(app_mod.subprocess, 'run',  # modified
                               return_value=mock.Mock(stdout='\n')):  # modified
            r = app_mod.Api.pick_folder({})  # modified
        self.assertFalse(r['ok'])  # modified
        self.assertIn('未选择', r['error'])  # modified


class BridgeRevTest(SessionMixin, unittest.TestCase):  # modified
    """页面轮询依据：只有插件推入才递增 bridge_rev。"""  # modified

    def test_only_bridge_requests_bump_rev(self):  # modified
        p = self.make_file()  # modified
        app_mod._SESSION.update({'sid': '', 'root': '', 'items': [],  # modified
                                 'focus': None, 'bridge_rev': 0})  # modified
        with mock.patch.object(app_mod.engine, 'check_root_allowed',  # modified
                               return_value=(True, '')):  # modified
            r = app_mod.Api.add_paths({'paths': [p], 'sid': '',  # modified
                                       'bridge': True})  # modified
            self.assertEqual(r['added'], 1)  # modified
            self.assertEqual(app_mod._SESSION['bridge_rev'], 1)  # modified
            # 页面自身再来一次，不应触发重复 preview
            app_mod.Api.add_paths({'paths': [p],  # modified
                                   'sid': app_mod._SESSION['sid']})  # modified
        self.assertEqual(app_mod._SESSION['bridge_rev'], 1)  # modified


class ModernDialogTest(SessionMixin, unittest.TestCase):  # modified
    """现代对话框：记住上次目录、取消不误弹、接口不可用自动回退。"""  # modified

    def test_last_folder_roundtrip(self):  # modified
        state = os.path.join(self.tmp, 'bridge_state.json')  # modified
        with mock.patch.object(bridge, 'STATE_FILE', state):  # modified
            self.assertEqual(bridge.load_last_folder(), '')  # modified
            bridge.save_last_folder(self.tmp)  # modified
            self.assertEqual(bridge.load_last_folder(), self.tmp)  # modified

    def test_last_folder_ignores_deleted_dir(self):  # modified
        state = os.path.join(self.tmp, 'bridge_state.json')  # modified
        with mock.patch.object(bridge, 'STATE_FILE', state):  # modified
            bridge.save_last_folder(os.path.join(self.tmp, 'gone'))  # modified
            self.assertEqual(bridge.load_last_folder(), '',  # modified
                             '目录已删除时不能把对话框定位到无效路径')  # modified

    def test_cancel_does_not_pop_a_second_dialog(self):  # modified
        with mock.patch.object(bridge, '_modern_dialog', return_value=[]), \
             mock.patch.object(bridge, '_legacy_pick_folder') as legacy:  # modified
            self.assertIsNone(bridge.pick_native_folder())  # modified
            legacy.assert_not_called()  # modified
        with mock.patch.object(bridge, '_modern_dialog', return_value=[]):  # modified
            self.assertEqual(bridge.pick_native_files(), [])  # modified

    @unittest.skipUnless(bridge.IS_WIN, 'Windows only')  # modified
    def test_unavailable_modern_falls_back_to_legacy(self):  # modified
        with mock.patch.object(bridge, '_modern_dialog', return_value=None), \
             mock.patch.object(bridge, '_legacy_pick_folder',  # modified
                               return_value='D:\\legacy'):  # modified
            self.assertEqual(bridge.pick_native_folder(), 'D:\\legacy')  # modified

    def test_pick_files_handles_unavailable_modern(self):  # modified
        with mock.patch.object(bridge, '_modern_dialog', return_value=None):  # modified
            self.assertEqual(bridge.pick_native_files(), [],  # modified
                             '接口不可用应安静返回空，不能崩')  # modified


class TopmostTest(unittest.TestCase):  # modified
    """对话框自动置顶：持续压住 z-order，且能被 stop 事件干净停下。"""  # modified

    @unittest.skipUnless(bridge.IS_WIN, 'Windows only')  # modified
    def test_pop_to_front_repeats_until_stopped(self):  # modified
        stop = threading.Event()  # modified
        calls = []  # modified

        def fake_setpos(hwnd, top, x, y, w, h, flags):  # modified
            calls.append((hwnd, top, flags))  # modified
            if len(calls) >= 3:  # modified
                stop.set()  # modified
            return True  # modified

        with mock.patch.object(bridge._user32, 'FindWindowW',  # modified
                               return_value=12345), \
             mock.patch.object(bridge._user32, 'SetWindowPos',  # modified
                               side_effect=fake_setpos), \
             mock.patch.object(bridge._user32, 'SetForegroundWindow',  # modified
                               return_value=True):  # modified
            ok = bridge._pop_to_front('任意标题', stop)  # modified
        self.assertTrue(ok)  # modified
        self.assertGreaterEqual(len(calls), 3,  # modified
                                'IFileDialog 会重设 z-order，必须持续置顶')  # modified
        self.assertEqual(calls[0][0], 12345)  # modified
        self.assertEqual(calls[0][1], bridge._HWND_TOPMOST)  # modified

    def test_start_topmost_returns_controllable_event(self):  # modified
        ev = bridge._start_topmost('不会匹配的标题')  # modified
        self.assertTrue(hasattr(ev, 'set'))  # modified
        ev.set()  # modified


if __name__ == '__main__':  # modified
    unittest.main(verbosity=2)  # modified
