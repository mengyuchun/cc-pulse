"""P0 止血修复 & P1 无门槛独立模式回归测试（纯标准库 unittest）。"""

import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import unittest.mock
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from cc_pulse import archive as ccpulse_archive
from cc_pulse import cli as ccpulse_cli
from cc_pulse import env as ccpulse_env
from cc_pulse import interactive as ccpulse_interactive
from cc_pulse import net as ccpulse_net
from cc_pulse import probe as ccpulse_probe
from cc_pulse import store as ccpulse_store

SCRIPT = os.path.join(_SRC, "cc_pulse", "cli.py")


class HelpSubcommandsTest(unittest.TestCase):
    """P0-1: 全部注册子命令 --help 正常退出，无 argparse % 格式崩溃。"""

    def test_all_subcommands_help_exit_zero(self):
        for cmd in (None, "check", "list-models", "inspect", "env-check", "trend"):
            args = [sys.executable, SCRIPT]
            if cmd:
                args.append(cmd)
            args.append("--help")
            proc = subprocess.run(
                args,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
            )
            self.assertEqual(
                proc.returncode,
                0,
                f"subcommand={cmd} help failed: {proc.stderr}",
            )
            self.assertNotIn("ValueError", proc.stderr)


class RedirectAndStreamSafetyTest(unittest.TestCase):
    """P0-7: HTTP 3xx 不跟随重定向（防 API Key 泄露）、异常脱敏、闭套接字 SSE 保护。"""

    def test_302_redirect_does_not_leak_auth_headers(self):
        sink_hits = []

        class RedirectHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                if length > 0:
                    self.rfile.read(length)
                if self.path.startswith("/evil_sink"):
                    sink_hits.append(dict(self.headers))
                    self.send_response(200)
                    self.send_header("Content-Length", "11")
                    self.end_headers()
                    self.wfile.write(b'{"ok":true}')
                    return
                self.send_response(302)
                self.send_header("Location", "/evil_sink")
                self.send_header("Content-Length", "0")
                self.end_headers()

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            url = f"http://127.0.0.1:{srv.server_port}/v1/messages"
            resp = ccpulse_net._http_request(
                url,
                method="POST",
                headers={"x-api-key": "sk-secret-redirect-123"},
                body=b"{}",
                timeout=5,
            )
            self.assertEqual(resp.status, 302)
            self.assertEqual(
                sink_hits, [], "302 redirect target must never be contacted"
            )

            p = ccpulse_probe.Provider(
                name="Redir",
                app_type="claude",
                base_url=f"http://127.0.0.1:{srv.server_port}",
                api_key="sk-secret-redirect-123",
                auth_mode="apikey",
                tiers=[
                    ccpulse_probe.ModelTier(
                        "default", "claude-sonnet-4-5", "claude-sonnet-4-5"
                    )
                ],
            )
            sr = ccpulse_probe.probe_stream(
                p, p.tiers[0], timeout=5, skip_tls_verify=False
            )
            self.assertEqual(sr["http_status"], 302)
            self.assertEqual(
                sink_hits, [], "probe_stream must not follow 302 redirects"
            )
        finally:
            srv.shutdown()
            srv.server_close()

    def test_drain_sse_stream_closed_socket_and_trailing_done(self):
        class FakeSocket:
            def fileno(self):
                return -1

            def gettimeout(self):
                return 30.0

            def settimeout(self, _val):
                raise OSError(10038, "Socket operation on nonsocket")

        class FakeRaw:
            _sock = FakeSocket()

        class FakeFP:
            raw = FakeRaw()

        class FakeHTTP10Stream:
            fp = FakeFP()

            def __iter__(self):
                # 第一条事件完整，末尾 data: [DONE] 无尾随双换行，模拟 HTTP/1.0 EOF 提前闭套接字
                yield b'data: {"model":"gpt-4o","choices":[{"delta":{"content":"5"}}]}\n\n'
                yield b"data: [DONE]"

        p = ccpulse_probe.Provider(
            name="SSEClose",
            app_type="codex",
            base_url="https://example.test",
            api_key="sk-test",
            auth_mode="bearer",
            protocol=ccpulse_probe.Protocol.OPENAI_CHAT_COMPLETIONS,
            tiers=[ccpulse_probe.ModelTier("default", "gpt-4o", "gpt-4o")],
        )
        res = ccpulse_probe._drain_sse_stream(
            FakeHTTP10Stream(),
            p,
            "openai_chat_completions",
            ttft_deadline=5.0,
            start=ccpulse_probe.time.time(),
        )
        self.assertEqual(res["text"], "5")
        self.assertFalse(res["stream_truncated"])


class InspectDedupAndContextGuardTest(unittest.TestCase):
    """P0-2 & P0-3 & P1-3: 单模型 inspect 协议透传、不隐式触发 context、云归因与分享卡片。"""

    def test_single_inspect_forwards_protocol_and_skips_implicit_context(self):
        class OpenAIChatWithHeadersHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'{"error":"not found"}')

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length).decode("utf-8", errors="replace")
                j = json.loads(body)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("x-amzn-requestid", "req-bedrock-123")
                self.end_headers()
                if "tools" in j:
                    self.wfile.write(
                        json.dumps(
                            {
                                "choices": [
                                    {
                                        "message": {
                                            "tool_calls": [
                                                {
                                                    "function": {
                                                        "name": "get_probe_number"
                                                    }
                                                }
                                            ]
                                        }
                                    }
                                ]
                            }
                        ).encode()
                    )
                    return
                # 正常 OpenAI Chat 响应（含 system_fingerprint / prompt_tokens）
                # 以前单模型 run_inspect 漏传 protocol 会误报 crosspack
                import re as _re

                txt = str((j.get("messages") or [{}])[0].get("content", ""))
                nums = [int(x) for x in _re.findall(r"\d+", txt)]
                ans = "5"
                if len(nums) >= 2:
                    if "minus" in txt or "减" in txt or "-" in txt:
                        ans = str(nums[0] - nums[1])
                    elif "times" in txt or "Multiply" in txt or "乘" in txt:
                        ans = str(nums[0] * nums[1])
                    else:
                        ans = str(nums[0] + nums[1])
                self.wfile.write(
                    json.dumps(
                        {
                            "model": j.get("model"),
                            "system_fingerprint": "fp_test",
                            "choices": [{"message": {"content": ans}}],
                            "usage": {
                                "prompt_tokens": 10,
                                "completion_tokens": 1,
                                "total_tokens": 11,
                            },
                        }
                    ).encode()
                )

        srv = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), OpenAIChatWithHeadersHandler
        )
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            env = dict(os.environ)
            env["CC_PULSE_TEST_KEY"] = "sk-super-secret-standalone"
            proc = subprocess.run(
                [
                    sys.executable,
                    SCRIPT,
                    "inspect",
                    "--no-db",
                    "--base-url",
                    f"http://127.0.0.1:{srv.server_port}",
                    "--api-key-env",
                    "CC_PULSE_TEST_KEY",
                    "--model",
                    "gpt-4o",
                    "--protocol",
                    "openai_chat",
                    "--include",
                    "text,metadata,tools",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                timeout=20,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            report = json.loads(proc.stdout)
            self.assertFalse(
                report["authenticity"]["crosspack"]["suspicious"],
                f"crosspack false positive: {report['authenticity']['crosspack']}",
            )
            self.assertEqual(report["context"]["status"], "skipped")
            self.assertEqual(report["backend_origin"]["origin"], "aws_bedrock")

            # 验证 --format card 输出 ≤25 行脱敏卡片且不含密钥或完整 URL
            proc_card = subprocess.run(
                [
                    sys.executable,
                    SCRIPT,
                    "inspect",
                    "--no-db",
                    "--base-url",
                    f"http://127.0.0.1:{srv.server_port}",
                    "--api-key-env",
                    "CC_PULSE_TEST_KEY",
                    "--model",
                    "gpt-4o",
                    "--protocol",
                    "openai_chat",
                    "--include",
                    "text,tools",
                    "--format",
                    "card",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                timeout=20,
                check=False,
            )
            self.assertEqual(proc_card.returncode, 0, proc_card.stderr)
            card_lines = proc_card.stdout.strip().splitlines()
            self.assertLessEqual(len(card_lines), 25)
            self.assertNotIn("sk-super-secret-standalone", proc_card.stdout)
            self.assertNotIn("127.0.0.1", proc_card.stdout)
            self.assertIn("AWS Bedrock", proc_card.stdout)
        finally:
            srv.shutdown()
            srv.server_close()


class StandaloneAndArchiveTempTest(unittest.TestCase):
    """P0-6 & P1-2: /tmp 归档、短密钥掩码、无 DB env-check 与动态题库。"""

    def test_archive_path_allows_tempdir(self):
        p = Path(tempfile.gettempdir()) / "ccpulse_test_hist.jsonl"
        resolved = ccpulse_archive.archive_path(str(p))
        self.assertTrue(resolved.endswith("ccpulse_test_hist.jsonl"))

    def test_short_key_mask_reveals_at_most_two_chars(self):
        self.assertEqual(ccpulse_env._mask("1234567"), "12***")
        self.assertEqual(ccpulse_env._mask("123456"), "***")

    def test_env_check_works_without_db(self):
        proc = subprocess.run(
            [sys.executable, SCRIPT, "env-check", "--no-db", "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
        self.assertIn(proc.returncode, (0, 2), proc.stderr)
        data = json.loads(proc.stdout)
        self.assertEqual(data["command"], "env-check")

    def test_dynamic_probe_prompt_varies(self):
        qs = {ccpulse_probe._pick_probe_prompt()["q"] for _ in range(15)}
        self.assertGreater(len(qs), 3)

    def test_standalone_providers_loader_reads_claude_and_toml(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".claude").mkdir()
            (home / ".claude" / "settings.json").write_text(
                json.dumps(
                    {
                        "env": {
                            "ANTHROPIC_BASE_URL": "https://claude.standalone.test",
                            "ANTHROPIC_AUTH_TOKEN": "sk-standalone-123456",
                            "ANTHROPIC_MODEL": "claude-sonnet-4-5",
                        }
                    }
                ),
                encoding="utf-8",
            )
            (home / ".cc-pulse").mkdir()
            (home / ".cc-pulse" / "providers.toml").write_text(
                '[providers.my_relay]\nbase_url = "https://toml.test"\napi_key = "sk-toml-123"\nmodel = "claude-haiku-4-5"\n',
                encoding="utf-8",
            )
            provs = ccpulse_store.load_standalone_providers(home)
            names = {p.name for p in provs}
            self.assertIn("claude-settings", names)
            self.assertIn("my_relay", names)

    def test_claude_code_headers_dynamic_os_and_arch(self):
        with (
            unittest.mock.patch(
                "cc_pulse.probe.platform.system", return_value="Darwin"
            ),
            unittest.mock.patch(
                "cc_pulse.probe.platform.machine", return_value="arm64"
            ),
        ):
            hdrs = ccpulse_probe._claude_code_headers(user_agent="test/1.0")
            self.assertEqual(hdrs["x-stainless-os"], "MacOS")
            self.assertEqual(hdrs["x-stainless-arch"], "arm64")

        with (
            unittest.mock.patch("cc_pulse.probe.platform.system", return_value="Linux"),
            unittest.mock.patch(
                "cc_pulse.probe.platform.machine", return_value="x86_64"
            ),
        ):
            hdrs = ccpulse_probe._claude_code_headers(user_agent="test/1.0")
            self.assertEqual(hdrs["x-stainless-os"], "Linux")
            self.assertEqual(hdrs["x-stainless-arch"], "x64")

        with (
            unittest.mock.patch(
                "cc_pulse.probe.platform.system", return_value="Windows"
            ),
            unittest.mock.patch(
                "cc_pulse.probe.platform.machine", return_value="AMD64"
            ),
        ):
            hdrs = ccpulse_probe._claude_code_headers(user_agent="test/1.0")
            self.assertEqual(hdrs["x-stainless-os"], "Windows")
            self.assertEqual(hdrs["x-stainless-arch"], "x64")

    def test_standalone_env_providers_reads_pulse_toml(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".cc-pulse").mkdir()
            (home / ".cc-pulse" / "providers.toml").write_text(
                '[providers.relay_x]\nbase_url = "https://toml.example.com"\napi_key = "sk-toml-secret-12345"\nsonnet = "claude-sonnet-4-5"\n',
                encoding="utf-8",
            )
            provs = ccpulse_env.load_standalone_env_providers(home)
            self.assertEqual(len(provs), 1)
            self.assertEqual(provs[0].name, "relay_x")
            findings = ccpulse_env.env_check_findings(
                provs, {"ANTHROPIC_BASE_URL": "https://override.example.com"}
            )
            self.assertEqual(findings[0]["severity"], "conflict")

    def test_cloud_backend_openai_official_with_resolved_protocol_and_card_link(self):
        p = ccpulse_probe.Provider(
            name="codex-auto",
            app_type="codex",
            base_url="https://api.openai.com",
            api_key="sk-test",
            auth_mode="bearer",
            tiers=[ccpulse_probe.ModelTier("default", "gpt-5", "gpt-5")],
            is_current=True,
            in_failover=True,
        )
        # app_type=codex 自动解析为 OPENAI_RESPONSES，即使 p.protocol 初始为 UNKNOWN
        bo = ccpulse_probe._detect_cloud_backend(
            {"openai-processing-ms": "120"},
            "{}",
            ccpulse_probe._resolve_protocol(p),
        )
        self.assertEqual(bo["origin"], "openai_official")
        card = ccpulse_cli.format_inspect_card(
            {
                "provider": "codex-auto",
                "model": "gpt-5",
                "protocol": {"detected": "openai_responses"},
                "backend_origin": bo,
                "summary": {"verdict": "healthy"},
            }
        )
        self.assertIn("https://github.com/mengyuchun/cc-pulse", card)

    def test_interactive_menu_dispatches_and_cleans_temp_key(self):
        calls = []
        seen_keys = []

        def fake_run(argv):
            calls.append(list(argv))
            seen_keys.append(os.environ.get(ccpulse_interactive._TEMP_KEY_ENV))
            return 0

        # 依次选 1(env-check) -> 2(单端点测活，输入 URL/protocol/model) -> 0(退出)
        answers = iter(["1", "2", "https://relay.test", "2", "claude-sonnet-4-5", "0"])
        rc = ccpulse_interactive.run_interactive_menu(
            fake_run,
            input_fn=lambda _p: next(answers),
            secret_fn=lambda _p: "sk-temp-menu-key",
            print_fn=lambda _s="": None,
            pause=False,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(calls[0], ["env-check"])
        self.assertEqual(
            calls[1],
            [
                "check",
                "--no-db",
                "--base-url",
                "https://relay.test",
                "--api-key-env",
                ccpulse_interactive._TEMP_KEY_ENV,
                "--protocol",
                "anthropic",
                "--model",
                "claude-sonnet-4-5",
            ],
        )
        self.assertEqual(seen_keys[1], "sk-temp-menu-key")
        self.assertNotIn(ccpulse_interactive._TEMP_KEY_ENV, os.environ)

    def test_should_open_interactive_menu_only_on_no_arg_tty(self):
        self.assertFalse(
            ccpulse_cli._should_open_interactive_menu(["cc-pulse", "check"])
        )
        with (
            unittest.mock.patch.object(sys.stdin, "isatty", return_value=True),
            unittest.mock.patch.object(sys.stdout, "isatty", return_value=True),
        ):
            self.assertTrue(ccpulse_cli._should_open_interactive_menu(["cc-pulse"]))
        with (
            unittest.mock.patch.object(sys.stdin, "isatty", return_value=False),
            unittest.mock.patch.object(sys.stdout, "isatty", return_value=True),
        ):
            self.assertFalse(ccpulse_cli._should_open_interactive_menu(["cc-pulse"]))


if __name__ == "__main__":
    unittest.main()
