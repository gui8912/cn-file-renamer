# -*- coding: utf-8 -*-
"""真实 HTTP 层回归：Host/Origin、静态路径、请求体边界与安全头。"""
import http.client
import json
import os
import socket
import sys
import threading
import unittest

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import app  # noqa: E402


class HttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = app.LocalServer(('127.0.0.1', 0), app.Handler)
        cls.port = cls.srv.server_port
        cls.t = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.t.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.t.join(timeout=3)

    def request(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        data = r.read()
        status, hdrs = r.status, dict(r.getheaders())
        c.close()
        return status, hdrs, data

    def test_ping_and_security_headers(self):
        status, headers, body = self.request('GET', '/api/ping')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['version'], app.VERSION)
        self.assertEqual(headers.get('X-Frame-Options'), 'DENY')
        self.assertIn('frame-ancestors', headers.get('Content-Security-Policy', ''))
        self.assertEqual(headers.get('X-Content-Type-Options'), 'nosniff')
        status, headers, _ = self.request('PUT', '/api/ping')
        self.assertEqual(status, 501)
        self.assertEqual(headers.get('X-Frame-Options'), 'DENY')

    def test_bad_host_and_origin(self):
        status, _, _ = self.request('GET', '/api/ping', headers={'Host': 'evil.example'})
        self.assertEqual(status, 403)
        status, _, _ = self.request('GET', '/api/ping',
                                    headers={'Origin': 'https://evil.example'})
        self.assertEqual(status, 403)
        status, _, _ = self.request('GET', '/api/ping',
                                    headers={'Host': '127.0.0.1:9999'})
        self.assertEqual(status, 403)

    def test_static_traversal(self):
        for p in ('/C:/Windows/win.ini', '/../app.py', '/%2e%2e/app.py'):
            status, _, _ = self.request('GET', p)
            self.assertIn(status, (400, 404), p)

    def test_json_content_type_and_errors(self):
        status, _, body = self.request(
            'POST', '/api/preview', body=b'{"enabled":{}}',
            headers={'Content-Type': 'text/plain'})
        self.assertEqual(status, 400)
        self.assertIn('Content-Type', json.loads(body)['error'])
        status, _, _ = self.request(
            'POST', '/api/preview', body=b'{bad',
            headers={'Content-Type': 'application/json'})
        self.assertEqual(status, 400)
        status, _, body = self.request(
            'POST', '/api/test-name', body=b'{"name":123}',
            headers={'Content-Type': 'application/json'})
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)['ok'])
        self.assertIn('字符串', json.loads(body)['error'])

    def test_oversize_body_returns_json_then_closes(self):
        body = b'{"x":"' + b'a' * (5 * 1024 * 1024) + b'"}'
        status, _, data = self.request(
            'POST', '/api/scan', body=body,
            headers={'Content-Type': 'application/json'})
        self.assertEqual(status, 400)
        self.assertIn('请求体过大', json.loads(data)['error'])

    def test_chunked_rejected(self):
        s = socket.create_connection(('127.0.0.1', self.port), timeout=5)
        req = (b'POST /api/scan HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n'
               b'Content-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n'
               b'2\r\n{}\r\n0\r\n\r\n') % self.port
        s.sendall(req)
        data = s.recv(4096)
        s.close()
        self.assertIn(b' 400 ', data.split(b'\r\n', 1)[0])

    def test_journal_id_traversal_business_rejected(self):
        status, _, body = self.request('GET', '/api/journal?id=../config')
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)['ok'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
