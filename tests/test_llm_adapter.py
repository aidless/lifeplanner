"""OpenAI 兼容适配器的集成测试。

用本地假 HTTP 服务器验证四件事：
    1. 请求/响应的解析（tool 调用从模型 JSON 里出来）
    2. JSON 模式被端点拒绝时自动降级重试
    3. 端点不可达时如实报 unavailable，而不是崩溃或装作可用
    4. pick_adapter 在配置了端点时优先选 openai-compat

不依赖真实的 Ollama —— 测试必须能在任何环境重复。
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from lifeplan.agent.adapters import OpenAICompatAdapter, pick_adapter
from lifeplan.db import init_db


class _FakeOpenAI(BaseHTTPRequestHandler):
    reject_json_mode = False
    calls: list[dict] = []

    def log_message(self, *args):  # 静默
        pass

    def _send(self, code: int, body: dict):
        data = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.endswith("/models"):
            self._send(200, {"data": [{"id": "fake-chat"}, {"id": "fake-embed"}]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        _FakeOpenAI.calls.append(body)

        if self.reject_json_mode and "response_format" in body:
            self._send(400, {"error": {"message": "response_format not supported"}})
            return
        content = json.dumps({"tool": "plan_score", "args": {}})
        self._send(200, {
            "choices": [{"message": {"role": "assistant", "content": content}}]
        })


class AdapterTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LIFEPLAN_HOME"] = self.tmp.name
        init_db()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOpenAI)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        self._old_env = {k: os.environ.get(k) for k in
                         ("LIFEPLAN_LLM_BASE_URL", "LIFEPLAN_LLM_MODEL",
                          "LIFEPLAN_LLM_API_KEY", "LIFEPLAN_JEV_CKPT")}
        os.environ["LIFEPLAN_LLM_BASE_URL"] = f"http://127.0.0.1:{self.port}/v1"
        os.environ["LIFEPLAN_LLM_MODEL"] = "fake-chat"
        os.environ.pop("LIFEPLAN_JEV_CKPT", None)
        _FakeOpenAI.reject_json_mode = False
        _FakeOpenAI.calls = []

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()


class TestOpenAICompat(AdapterTestCase):
    def test_status_uses_configured_endpoint(self):
        ad = OpenAICompatAdapter()
        ok, why = ad.status()
        self.assertTrue(ok)
        self.assertIn("fake-chat", why)

    def test_decide_parses_tool_call(self):
        ad = OpenAICompatAdapter()
        d = ad.decide("现在状态怎么样", [], 0)
        self.assertEqual(d.tool, "plan_score")

    def test_json_mode_fallback_on_400(self):
        _FakeOpenAI.reject_json_mode = True
        ad = OpenAICompatAdapter()
        d = ad.decide("现在状态怎么样", [], 0)
        self.assertEqual(d.tool, "plan_score", "JSON 模式被拒后应降级重试成功")
        # 第一次带 response_format（被拒），第二次不带（成功）
        self.assertTrue(any("response_format" in c for c in _FakeOpenAI.calls))
        self.assertTrue(any("response_format" not in c for c in _FakeOpenAI.calls))

    def test_unreachable_endpoint_reports_honestly(self):
        # 找一个确定没有监听的端口
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]
        s.close()
        os.environ["LIFEPLAN_LLM_BASE_URL"] = f"http://127.0.0.1:{free_port}/v1"
        ad = OpenAICompatAdapter()
        ok, why = ad.status()
        self.assertFalse(ok)
        self.assertIn("无法访问", why)

    def test_model_not_in_list(self):
        os.environ["LIFEPLAN_LLM_MODEL"] = "不存在的模型"
        ad = OpenAICompatAdapter()
        ok, why = ad.status()
        self.assertFalse(ok)
        self.assertIn("不在端点列表", why)


class TestPickAdapter(AdapterTestCase):
    def test_openai_preferred_when_configured(self):
        """配置了 openai 端点时优先选它——但本机若装着 laya，
        systemone 会抢先，所以测试里显式让 systemone 不可用。"""
        import lifeplan.agent.adapters as A

        orig = A.SystemOneAdapter.status
        A.SystemOneAdapter.status = lambda self: (False, "测试：模拟不可用")
        try:
            ad, skipped = pick_adapter()
            self.assertEqual(ad.name, "openai-compat")
            self.assertTrue(any(s.startswith("jev-local") for s in skipped))
        finally:
            A.SystemOneAdapter.status = orig

    def test_decide_via_pick(self):
        ad, _ = pick_adapter()
        d = ad.decide("状态如何", [], 0)
        self.assertEqual(d.tool, "plan_score")


if __name__ == "__main__":
    unittest.main()
