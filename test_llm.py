# -*- coding: utf-8 -*-
"""LLM 层安全回归：Key 保留、授权目录、批量上限与配置类型。"""
import os
import shutil
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
from core import engine, llm  # noqa: E402

TMP = tempfile.mkdtemp(prefix='rename_llm_test_')


class LlmTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old_llm = llm.CONF
        cls.old_engine_cfg = engine.CONFIG
        llm.CONF = os.path.join(TMP, 'llm.json')
        engine.CONFIG = os.path.join(TMP, 'config.json')
        cls.root = os.path.join(TMP, 'allowed')
        cls.outside = os.path.join(TMP, 'outside')
        os.makedirs(cls.root)
        os.makedirs(cls.outside)
        with open(os.path.join(cls.root, 'ok.txt'), 'w', encoding='utf-8') as f:
            f.write('AUTHORIZED-CONTENT')
        with open(os.path.join(cls.outside, 'secret.txt'), 'w', encoding='utf-8') as f:
            f.write('SECRET-MUST-NOT-LEAVE')
        engine.save_config({'trusted_roots': [cls.root], 'max_scan': 5000,
                            'journal_keep': 300, 'lower_ext': False})

    @classmethod
    def tearDownClass(cls):
        engine.save_config({'trusted_roots': [], 'max_scan': 5000,
                            'journal_keep': 300, 'lower_ext': False})
        llm.CONF = cls.old_llm
        engine.CONFIG = cls.old_engine_cfg
        shutil.rmtree(TMP, ignore_errors=True)

    def tearDown(self):
        if os.path.exists(llm.CONF):
            os.remove(llm.CONF)

    def test_api_key_is_preserved_when_omitted(self):
        llm.save_conf({'enabled': True, 'api_key': 'sk-secret'})
        llm.save_conf({'enabled': False, 'prompt': 'new'})
        cfg = llm.load_conf()
        self.assertEqual(cfg['api_key'], 'sk-secret')
        self.assertFalse(cfg['enabled'])
        llm.save_conf({'api_key': None})
        self.assertEqual(llm.load_conf()['api_key'], '')

    def test_enabled_string_false_is_false(self):
        llm.save_conf({'enabled': 'false'})
        self.assertFalse(llm.load_conf()['enabled'])
        self.assertFalse(llm.is_ready(llm.load_conf()))

    def test_timeout_batch_clamped(self):
        cfg = llm.save_conf({'timeout': -1, 'batch_size': 9999, 'temperature': 99})
        self.assertEqual(cfg['timeout'], 5)
        self.assertEqual(cfg['batch_size'], 100)
        self.assertEqual(cfg['temperature'], 2.0)

    def test_unauthorized_file_content_not_sent(self):
        llm.save_conf({'enabled': True, 'base_url': 'http://127.0.0.1:1/v1',
                       'model': 'mock', 'api_key': 'sk'})
        captured = []

        def fake_chat(messages, cfg):
            captured.append(messages[1]['content'])
            return '["新名字"]'

        old = llm._chat
        llm._chat = fake_chat
        try:
            res = llm.suggest(
                ['授权文件', '越权文件'], cfg=llm.load_conf(), with_content=True,
                paths=[os.path.join(self.root, 'ok.txt'),
                       os.path.join(self.outside, 'secret.txt')])
        finally:
            llm._chat = old
        self.assertTrue(res['ok'], res)
        self.assertEqual(len(captured), 1)
        self.assertIn('AUTHORIZED-CONTENT', captured[0])
        self.assertNotIn('SECRET-MUST-NOT-LEAVE', captured[0])
        self.assertTrue(res.get('skipped_content'))

    def test_too_many_stems(self):
        llm.save_conf({'enabled': True, 'base_url': 'http://127.0.0.1:1/v1',
                       'model': 'mock'})
        res = llm.suggest(['x'] * 201, cfg=llm.load_conf())
        self.assertFalse(res['ok'])
        self.assertIn('200', res['error'])

    def test_docx_snippet_is_capped(self):
        import zipfile
        p = os.path.join(self.root, 'big.docx')
        with zipfile.ZipFile(p, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr('word/document.xml', '<w:p>' + ('字' * 3_000_000) + '</w:p>')
        text = llm.read_snippet(p, max_chars=600)
        self.assertLessEqual(len(text), 600)


if __name__ == '__main__':
    unittest.main(verbosity=2)
