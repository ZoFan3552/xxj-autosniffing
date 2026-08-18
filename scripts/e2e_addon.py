#!/usr/bin/env python3
"""端到端检查 addon_bridge.py：启动真实的 mitmproxy，用本地上游服务验证四条路径。

覆盖：本地加解密的流量记录、Mock 规则的直接应答与局部改写、代发请求、
WebSocket 帧记录与回放。

用法：
    python scripts/e2e_addon.py

依赖：mitmproxy、httpx、cryptography（addon 本身就需要），以及 websockets（仅 WS 用例，
缺失时该组用例会被跳过）。
"""

import asyncio
import base64
import importlib.util
import json
import os
import queue
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
BRIDGE = REPO_ROOT / "src-tauri" / "src" / "python" / "addon_bridge.py"
# 默认用当前解释器跑源码；给 XXJ_ADDON_CMD 指一个 PyInstaller 打好的 sidecar，
# 就能用同一套用例验证打包产物（漏 import 只有这样才查得出来）。
ADDON_CMD = ([os.environ["XXJ_ADDON_CMD"]] if os.environ.get("XXJ_ADDON_CMD")
             else [sys.executable, str(BRIDGE)])
SECRET = b"e2e-secret"
PROXY_PORT = 18081
UP_PORT = 18082
WS_PROXY_PORT = 18091
WS_PORT = 18092
WS_PATH = "/chat"

# 直接加载 addon 模块借用它的本地加解密，用来构造密文体与校验响应。
# 关掉字节码写入，免得这次导入在仓库里留下 __pycache__ 改动。
sys.dont_write_bytecode = True
_spec = importlib.util.spec_from_file_location("addon_bridge", BRIDGE)
bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge)


def enc(text: str) -> str:
    return bridge._local_encrypt(text, SECRET)


def dec(cipher: str) -> str:
    return bridge._local_decrypt(cipher, SECRET)


class Proxy:
    """一个跑起来的 addon_bridge 子进程，外加它 stdout 事件的读取与等待。"""

    def __init__(self, config: dict):
        self.proc = subprocess.Popen(
            [*ADDON_CMD, "--config", json.dumps(config)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.events: queue.Queue = queue.Queue()
        self.seen: list[dict] = []
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self):
        for line in self.proc.stdout:
            if line.strip().startswith("{"):
                self.events.put(json.loads(line))

    def _read_stderr(self):
        for line in self.proc.stderr:
            print("[stderr]", line, end="")

    def send(self, command: dict):
        self.send_line(json.dumps(command))

    def send_line(self, line: str):
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def wait_for(self, pred, what: str, timeout: float = 25) -> dict:
        """先扫已收到的事件，再继续读——事件可能早于这次等待就到了。"""
        for event in self.seen:
            if pred(event):
                return event
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                event = self.events.get(timeout=max(0.01, deadline - time.time()))
            except queue.Empty:
                break
            self.seen.append(event)
            if event.get("event") == "log" and event["data"].get("level") in ("error", "warn"):
                print("  log:", event["data"]["msg"])
            if pred(event):
                return event
        raise AssertionError(f"等待 {what} 超时；最后几条事件：{self.seen[-6:]}")

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


UPSTREAM_BODY = json.dumps({"a": 1, "b": {"c": 2}, "keep": "yes"}, ensure_ascii=False)
REQUEST_PLAIN = json.dumps({"base": {"traceId": "abc", "timestamp": "1"}, "data": {"q": 1}})


def run_http_checks() -> None:
    hits: list[tuple[str, bytes]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("content-length") or 0)
            hits.append((self.path, self.rfile.read(length)))
            body = enc(UPSTREAM_BODY).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST

        def log_message(self, *args):
            pass

    upstream = HTTPServer(("127.0.0.1", UP_PORT), Handler)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    proxy = Proxy({
        "proxy_port": PROXY_PORT,
        "capture_hosts": ["127.0.0.1"],
        "breakpoints": [],
        "mock_rules": [
            {"id": "1", "name": "respond-once", "url_pattern": "/mock", "method": "POST",
             "mode": "respond", "status": 201, "body": '{"mocked": true}',
             "times": 1, "delay_ms": 0, "encrypt": True, "enabled": True},
            {"id": "2", "name": "patch-all", "url_pattern": "/patch", "method": "",
             "mode": "patch", "status": 200, "body": '{"b": {"c": 3}, "keep": null, "new": 9}',
             "times": 0, "delay_ms": 0, "encrypt": True, "enabled": True},
        ],
    })
    try:
        proxy.wait_for(lambda e: e.get("event") == "status" and e["data"].get("running"), "代理就绪")
        # 密钥只经 stdin 下发，不进命令行参数
        proxy.send({"command": "update_config", "encrypt_url": "", "decrypt_url": "",
                    "crypto_secret": base64.b64encode(SECRET).decode(), "crypto_secret_b64": True})
        proxy.wait_for(lambda e: e.get("event") == "log" and "config updated" in e["data"].get("msg", ""),
                       "配置生效")

        import httpx
        client = httpx.Client(proxy=f"http://127.0.0.1:{PROXY_PORT}", timeout=20, trust_env=False)
        base = f"http://127.0.0.1:{UP_PORT}"

        # 1. 普通记录：请求体用本地密钥解密后落进记录
        response = client.post(f"{base}/normal", content=enc(REQUEST_PLAIN).encode())
        assert response.status_code == 200, response.status_code
        assert dec(response.text) == UPSTREAM_BODY
        event = proxy.wait_for(lambda e: e.get("event") == "record" and e["data"]["path"] == "/normal"
                               and e["data"]["status"] == "complete", "/normal 的记录")
        assert event["data"]["request_plain"] == REQUEST_PLAIN, event["data"]["request_plain"]
        assert json.loads(event["data"]["response_plain"]) == json.loads(UPSTREAM_BODY)
        assert event["data"]["origin"] == "device" and event["data"]["mock"] is None
        print("1. 流量记录 + 本地解密            OK")

        # 2. 直接应答：不发到服务端，且只生效一次
        before = len(hits)
        response = client.post(f"{base}/mock", content=enc(REQUEST_PLAIN).encode())
        assert response.status_code == 201, response.status_code
        assert json.loads(dec(response.text)) == {"mocked": True}
        assert len(hits) == before, "直接应答不应到达服务端"
        event = proxy.wait_for(lambda e: e.get("event") == "record" and e["data"]["path"] == "/mock"
                               and e["data"]["status"] == "complete", "/mock 的记录")
        assert event["data"]["mock"] == {"rule": "respond-once", "mode": "respond"}, event["data"]["mock"]
        response = client.post(f"{base}/mock", content=enc(REQUEST_PLAIN).encode())
        assert len(hits) == before + 1, "times:1 用尽后第二次应当照常发到服务端"
        assert dec(response.text) == UPSTREAM_BODY
        print("2. 直接应答 + 命中次数上限        OK")

        # 3. 局部改写：片段合并进真实响应
        response = client.post(f"{base}/patch", content=enc(REQUEST_PLAIN).encode())
        assert json.loads(dec(response.text)) == {"a": 1, "b": {"c": 3}, "new": 9}, dec(response.text)
        event = proxy.wait_for(lambda e: e.get("event") == "record" and e["data"]["path"] == "/patch"
                               and e["data"]["status"] == "complete", "/patch 的记录")
        assert event["data"]["mock"] == {"rule": "patch-all", "mode": "patch"}
        assert json.loads(event["data"]["response_plain"]) == {"a": 1, "b": {"c": 3}, "new": 9}
        print("3. 局部改写（RFC 7386 合并）      OK")

        # 4. 代发请求：由代理自己发出，记录里 origin 为 outbound
        proxy.send({"command": "outbound", "id": "ob-1", "url": f"{base}/outbound", "method": "POST",
                    "headers": {"Authorization": "Bearer t", "Content-Type": "text/plain"},
                    "body_plain": REQUEST_PLAIN, "encrypt": True})
        event = proxy.wait_for(lambda e: e.get("event") == "outbound_result", "代发结果")
        assert event["data"]["error"] is None, event["data"]["error"]
        assert event["data"]["response_status"] == 200
        assert json.loads(event["data"]["response_plain"]) == json.loads(UPSTREAM_BODY)
        assert dec(hits[-1][1].decode()) == REQUEST_PLAIN, "服务端收到的应当是密文体"
        event = proxy.wait_for(lambda e: e.get("event") == "record" and e["data"]["path"] == "/outbound"
                               and e["data"]["status"] == "complete", "/outbound 的记录")
        assert event["data"]["origin"] == "outbound", event["data"]["origin"]
        assert "x-xxj-origin" not in {k.lower() for k in event["data"]["request_headers"]}, \
            "自报家门的头必须在转发前摘掉"
        print("4. 代发请求                       OK")
    finally:
        proxy.stop()
        upstream.shutdown()


def run_breakpoint_checks() -> None:
    """断点改写后重新加密时，密文体的形态（裸 base64 / 带引号）要跟随被替换的原密文体。"""
    hits: list[bytes] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("content-length") or 0)
            hits.append(self.rfile.read(length))
            body = enc(UPSTREAM_BODY).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    upstream = HTTPServer(("127.0.0.1", UP_PORT + 10), Handler)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    proxy = Proxy({
        "proxy_port": PROXY_PORT + 10,
        "capture_hosts": ["127.0.0.1"],
        "breakpoints": [{"url_pattern": "/bp", "break_request": True,
                         "break_response": False, "enabled": True}],
        "mock_rules": [],
    })
    try:
        proxy.wait_for(lambda e: e.get("event") == "status" and e["data"].get("running"), "代理就绪")
        proxy.send({"command": "update_config", "encrypt_url": "", "decrypt_url": "",
                    "crypto_secret": base64.b64encode(SECRET).decode(), "crypto_secret_b64": True})
        proxy.wait_for(lambda e: e.get("event") == "log" and "config updated" in e["data"].get("msg", ""),
                       "配置生效")

        import httpx
        client = httpx.Client(proxy=f"http://127.0.0.1:{PROXY_PORT + 10}", timeout=20, trust_env=False)
        base = f"http://127.0.0.1:{UP_PORT + 10}"
        edited = json.dumps({"edited": True})

        for label, wire in (("裸 base64", enc(REQUEST_PLAIN)), ("带引号", f'"{enc(REQUEST_PLAIN)}"')):
            done = threading.Event()
            result: dict = {}

            def call():
                result["response"] = client.post(f"{base}/bp", content=wire.encode())
                done.set()

            threading.Thread(target=call, daemon=True).start()
            event = proxy.wait_for(lambda e: e.get("event") == "intercept"
                                   and e["data"]["phase"] == "request", f"{label}的待决策项")
            assert event["data"]["body_plain"] == REQUEST_PLAIN, event["data"]["body_plain"]
            proxy.send_line("intercept_respond\t{}\tpass\t{}".format(
                event["data"]["flow_id"], base64.b64encode(edited.encode()).decode()))
            assert done.wait(20), f"{label}：放行后请求没有完成"
            assert result["response"].status_code == 200
            received = hits[-1].decode()
            assert received.startswith('"') == wire.startswith('"'), \
                f"{label}：重新加密后的密文体形态应与原密文体一致，实际收到 {received[:8]}"
            assert dec(received) == edited, f"{label}：服务端收到的不是改写后的内容"
            # 下一轮要重新等一条新的待决策项，清掉已消费的事件
            proxy.seen = [e for e in proxy.seen if e.get("event") != "intercept"]
        print("5. 断点改写 + 密文体形态跟随     OK")
    finally:
        proxy.stop()
        upstream.shutdown()


def run_ws_checks() -> None:
    try:
        import websockets
    except ImportError:
        print("跳过 WebSocket 用例：未安装 websockets（pip install websockets）")
        return

    server_saw: list[str] = []

    async def handler(ws):
        # 握手后主动推一帧：回放期它必须被吞掉，不能混进注入的帧里
        await ws.send("server-push")
        async for message in ws:
            server_saw.append(message)
            await ws.send(f"reply-1:{message}")
            await asyncio.sleep(0.15)
            await ws.send(f"reply-2:{message}")

    async def main():
        proxy = Proxy({"proxy_port": WS_PROXY_PORT, "capture_hosts": ["127.0.0.1"],
                       "breakpoints": [], "mock_rules": []})
        try:
            async with websockets.serve(handler, "127.0.0.1", WS_PORT):
                proxy.wait_for(lambda e: e.get("event") == "status" and e["data"].get("running"), "代理就绪")
                url = f"ws://127.0.0.1:{WS_PORT}{WS_PATH}"
                via = f"http://127.0.0.1:{WS_PROXY_PORT}"

                # 6. 录一轮对话
                async with websockets.connect(url, proxy=via) as ws:
                    got = [await asyncio.wait_for(ws.recv(), 10)]
                    await ws.send("hello")
                    got += [await asyncio.wait_for(ws.recv(), 10),
                            await asyncio.wait_for(ws.recv(), 10)]
                assert got == ["server-push", "reply-1:hello", "reply-2:hello"], got
                assert server_saw == ["hello"], server_saw

                conn = proxy.wait_for(lambda e: e.get("event") == "ws_conn"
                                      and e["data"]["state"] == "open", "WS 连接建立")
                assert conn["data"]["path"] == WS_PATH, conn["data"]["path"]
                proxy.wait_for(lambda e: e.get("event") == "ws_frame" and e["data"]["dir"] == "down"
                               and e["data"]["payload"] == "reply-2:hello", "第二个下行帧")
                frames = [e["data"] for e in proxy.seen
                          if e.get("event") == "ws_frame" and e["data"]["conn"] == 1]
                assert [f["dir"] for f in frames] == ["down", "up", "down", "down"], \
                    [f["dir"] for f in frames]
                assert frames[1]["payload"] == "hello" and frames[1]["type"] == "text"
                # 帧的时间偏移要反映服务端两次应答之间等的那 150 ms
                gap = frames[3]["t_ms"] - frames[2]["t_ms"]
                assert 100 < gap < 400, f"两次应答之间的间隔是 {gap} ms"
                print("6. WS 帧记录（含时间偏移）        OK")

                # 7. 装填回放后重连：上行帧被吞、真实下行帧也被吞、录好的下行帧按原间隔注回
                proxy.send({"command": "ws_replay", "path": WS_PATH, "frames": frames})
                proxy.wait_for(lambda e: e.get("event") == "log"
                               and "ws replay armed" in e["data"].get("msg", ""), "回放装填")

                server_saw.clear()
                started = time.monotonic()
                async with websockets.connect(url, proxy=via) as ws:
                    await ws.send("this must not reach the server")
                    replayed = [await asyncio.wait_for(ws.recv(), 10) for _ in range(3)]
                    # 真实服务端在这条连接上也推了 server-push，它必须被吞掉，
                    # 所以这里不该再有第四帧
                    try:
                        extra = await asyncio.wait_for(ws.recv(), 1.0)
                        raise AssertionError(f"回放期收到了真实服务端的帧：{extra}")
                    except TimeoutError:
                        pass
                elapsed = time.monotonic() - started
                assert replayed == ["server-push", "reply-1:hello", "reply-2:hello"], replayed
                assert server_saw == [], f"上行帧应被吞掉，但服务端收到了 {server_saw}"
                assert elapsed > 0.1, f"回放 {elapsed:.3f}s 内跑完，没有按原间隔注入"
                proxy.wait_for(lambda e: e.get("event") == "ws_frame" and e["data"]["conn"] == 2
                               and e["data"]["injected"] and e["data"]["payload"] == "reply-2:hello",
                               "注入帧被记录")
                injected = [e["data"] for e in proxy.seen if e.get("event") == "ws_frame"
                            and e["data"]["conn"] == 2 and e["data"]["injected"]]
                assert len(injected) == 3, injected
                print("7. WS 回放（吞上下行、注入录制帧） OK")

                # 8. 回放只生效一次，下一条连接回到真实服务端
                server_saw.clear()
                async with websockets.connect(url, proxy=via) as ws:
                    assert await asyncio.wait_for(ws.recv(), 10) == "server-push"
                    await ws.send("back to live")
                    assert await asyncio.wait_for(ws.recv(), 10) == "reply-1:back to live"
                assert server_saw == ["back to live"], server_saw
                print("8. 回放只消费一次                 OK")
        finally:
            proxy.stop()

    asyncio.run(main())


if __name__ == "__main__":
    run_http_checks()
    run_breakpoint_checks()
    run_ws_checks()
    print("\n全部端到端用例通过")
