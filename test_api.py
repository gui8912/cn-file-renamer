# -*- coding: utf-8 -*-
"""API 业务层回归，不启动端口、不依赖第三方包。"""
import json
import os
import shutil
import sys
import unittest

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import app  # noqa: E402
from core import engine, rules  # noqa: E402

ROOT = os.path.join(BASE, '_test_api')


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        shutil.rmtree(ROOT, ignore_errors=True)
        os.makedirs(ROOT)
        cls.old_presets = app.PRESETS
        cls.old_session = dict(app._SESSION)
        app.PRESETS = os.path.join(ROOT, 'presets.json')

    @classmethod
    def tearDownClass(cls):
        app.PRESETS = cls.old_presets
        app._SESSION.clear()
        app._SESSION.update(cls.old_session)
        shutil.rmtree(ROOT, ignore_errors=True)

    def setUp(self):
        if os.path.exists(app.PRESETS):
            os.remove(app.PRESETS)
        app._SESSION.update({'root': ROOT, 'items': [], 'focus': None, 'sid': ''})

    def test_preset_full_roundtrip(self):
        enabled = {r['id']: False for r in rules.RULES}
        saved = app.Api.presets_save({
            'name': '测试方案', 'enabled': enabled, 'params': {},
            'lower_ext': True, 'auto_dedupe': True,
            'sort_by': 'mtime', 'sort_desc': True, 'target': 'both',
        })
        self.assertTrue(saved['ok'], saved)
        self.assertIsInstance(saved['presets'], list)
        p = saved['presets'][0]
        self.assertEqual(p['name'], '测试方案')
        self.assertTrue(p['lower_ext'])
        self.assertTrue(p['auto_dedupe'])
        self.assertEqual(p['sort_by'], 'mtime')
        self.assertEqual(p['target'], 'both')
        self.assertFalse(any(p['enabled'].values()))
        loaded = app.Api.presets_get()
        self.assertEqual(loaded['presets'][0]['name'], '测试方案')
        deleted = app.Api.presets_delete({'name': '测试方案'})
        self.assertTrue(deleted['ok'])
        self.assertEqual(deleted['presets'], [])

    def test_preview_all_rules_off_means_off(self):
        path = os.path.join(ROOT, '關於第一百一十條.txt')
        with open(path, 'w', encoding='utf-8') as f:
            f.write('x')
        app._SESSION['items'] = [{
            'name': os.path.basename(path), 'dir': ROOT, 'path': path,
            'is_dir': False, 'size': 1, 'mtime': 1,
        }]
        res = app.Api.preview({'enabled': {}, 'params': {}})
        self.assertTrue(res['ok'], res)
        self.assertEqual(res['rows'][0]['new_name'], os.path.basename(path))
        self.assertEqual(res['rows'][0]['status'], 'unchanged')
        single = app.Api.test_name({'name': '第一百一十条', 'enabled': {}, 'params': {}})
        self.assertEqual(single['result'], '第一百一十条')

    def test_stale_session_is_rejected(self):
        app._SESSION['sid'] = 'new-session'
        res = app.Api.preview({'sid': 'old-session', 'enabled': {}, 'params': {}})
        self.assertFalse(res['ok'])
        self.assertTrue(res.get('stale'))
        for sid in ('old-session', '', None):
            clear = app.Api.clear_list({'sid': sid})
            self.assertFalse(clear['ok'], sid)
            self.assertTrue(clear.get('stale'), sid)
        # 显式 new_session 是右键新标签页，允许接管但必须换 sid。
        old_sid = app._SESSION['sid']
        res = app.Api.add_paths({'paths': [], 'new_session': True})
        self.assertTrue(res['ok'])
        self.assertNotEqual(res['sid'], old_sid)
        app._SESSION['sid'] = ''

    def test_dirty_preset_entry_does_not_break_list(self):
        with open(app.PRESETS, 'w', encoding='utf-8') as f:
            json.dump({'坏方案': 'oops'}, f, ensure_ascii=False)
        res = app.Api.presets_get()
        self.assertTrue(res['ok'])
        self.assertEqual(res['presets'], [])

    def test_system_subtree_cannot_be_authorized(self):
        root = os.environ.get('SystemRoot', r'C:\Windows')
        res = app.Api.add_root(os.path.join(root, 'System32'))
        self.assertFalse(res['ok'])
        self.assertIn('子目录', res['error'])

    def test_invalid_preset_name(self):
        self.assertFalse(app.Api.presets_save({'name': ''})['ok'])
        self.assertFalse(app.Api.presets_save({'name': 'x' * 51})['ok'])

    def test_journal_id_rejects_traversal(self):
        self.assertIsNone(engine.get_journal('../config'))
        self.assertIsNone(engine.get_journal('..\\..\\config'))
        self.assertIsNone(engine.get_journal('a/b'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
