# Copyright (c) 2026 Gecheng Fu. SPDX-License-Identifier: MIT
"""Real loopback HTTP regression checks; database and embedding stay isolated."""
import http.client
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('mem_server_test', os.environ.get('PGMEMREST_TEST_SOURCE', str(Path(__file__).resolve().parents[1] / 'mem_server/server.py')))
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)

class JsonBodyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.multiple(server, DB_PATH=str(Path(self.tmp.name)/'isolated.db'), API_KEY='', EMBED_BASE_URL='', EMBED_API_KEY='', EMBED_MODEL='')
        patcher.start()
        self.addCleanup(patcher.stop)
        server.reset_db()
        self.httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.worker = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.worker.join(timeout=3)

    def request(self, body, length=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.httpd.server_port, timeout=3)
        try:
            headers = {'Content-Type': 'application/json'}
            if length is not None:
                headers['Content-Length'] = length
            conn.request('POST', '/v1/reset', body=body, headers=headers)
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def test_bad_json_never_reaches_reset(self):
        for body in [b'{', b'\xff', b'[]', b'null', b'"text"', b'42', b'true']:
            with self.subTest(body=body), patch.object(server, 'reset_db') as reset:
                status, payload = self.request(body)
                self.assertEqual(status, 400)
                self.assertIn('error', payload)
                reset.assert_not_called()

    def test_bad_content_length(self):
        for length in ['abc', '-1']:
            with self.subTest(length=length), patch.object(server, 'reset_db') as reset:
                status, _ = self.request(b'', length)
                self.assertEqual(status, 400)
                reset.assert_not_called()

    def test_object_and_empty_body_still_work(self):
        for body in [b'{}', b'']:
            with self.subTest(body=body):
                status, payload = self.request(body)
                self.assertEqual(status, 200)
                self.assertEqual(payload['status'], 'reset')

if __name__ == '__main__':
    unittest.main()
