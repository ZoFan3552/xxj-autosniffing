"""
Standalone Python script for running mitmproxy as a subprocess.
Events are emitted as JSON lines to stdout.
Commands are received as JSON lines on stdin.

Mock rules (checked first, before breakpoints):
  - "respond" mode returns the configured body without contacting the server
  - "patch" mode forwards the request, then merges a JSON fragment into the real response
  - Each rule may cap its hit count (times) and delay its reply (delay_ms)

Charles-like breakpoint system:
  - Multiple breakpoint rules, each with url_pattern + break_request + break_response + enabled
  - When a request/response matches an enabled rule, it is paused via flow.intercept()
  - The frontend can edit the body and pass/abort
  - Uses mitmproxy's native flow.intercept() / flow.resume() / flow.wait_for_resume()

Crypto backends, in priority order:
  1. Local secret — AES-128-ECB over gzip, in-process, no external service
  2. Encrypt/decrypt HTTP endpoints
  3. Neither configured → bodies pass through as plain text

WebSocket:
  - Every frame of a captured connection is emitted as a "ws_frame" event
  - A connection whose path has a replay armed gets its upstream frames dropped and
    the recorded downstream frames injected back at their original relative times

Outbound requests:
  - The frontend can ask this process to send a request through the proxy itself,
    so it is encrypted, recorded and matched against mock rules like device traffic

Lifecycle:
  1. request phase  → emit "record" with status="pending", check mock rules then breakpoints
  2. response phase → emit "record" with status="complete", check mock rules then breakpoints
  3. error phase    → emit "record" with status="error"
"""
import asyncio
import base64
import contextlib
import gzip
import hashlib
import io
import logging
import re
import sys
import json
import time
import threading
from collections import OrderedDict
from urllib.parse import urlparse
import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7
from mitmproxy import http
from mitmproxy.options import Options
from mitmproxy.tools.dump import DumpMaster

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

_master = None
_encrypt_url = ""
_decrypt_url = ""
# Raw secret for local crypto; None means fall back to the HTTP endpoints.
_crypto_secret: bytes | None = None
_capture_hosts: list[str] = []
_breakpoints: list[dict] = []  # [{url_pattern, break_request, break_response, enabled}]
_mock_rules: list[dict] = []  # [{id, name, url_pattern, method, mode, status, body, times, delay_ms, encrypt, enabled}]
_proxy_port = 8080
_loop: asyncio.AbstractEventLoop | None = None

# Hit counters for mock rules, keyed by rule fingerprint so that editing one rule
# does not reset the counters of the others.
_mock_used: dict[str, int] = {}
_mock_lock = threading.Lock()

# WebSocket replays armed by the frontend, keyed by URL path. Taken (and removed)
# by the next connection that handshakes on that path.
_ws_replays: dict[str, list[dict]] = {}
_ws_lock = threading.Lock()

# Outbound requests announce themselves with this header so the addon can mark
# their origin and skip breakpoints; it is stripped before forwarding upstream.
ORIGIN_HEADER = "x-xxj-origin"
ORIGIN_OUTBOUND = "outbound"

_async_client: httpx.AsyncClient | None = None

# Maps intercept_id → (flow, phase) for pending intercepts
_intercepted_flows: dict[str, tuple[http.HTTPFlow, str]] = {}
# Maps intercept_id → (action, body) for completed intercept responses
_intercept_results: dict[str, tuple[str, str | None]] = {}
_intercept_result_times: dict[str, float] = {}
_intercept_waiters: dict[str, asyncio.Event] = {}
_intercept_lock = threading.Lock()

# Protects writes to the config globals (_encrypt_url, _decrypt_url,
# _capture_hosts, _breakpoints) which are written by the stdin thread
# and read by the asyncio event-loop thread.
# Readers take a short snapshot (local binding) so they hold the lock only
# briefly and never across an `await`.
_config_lock = threading.Lock()

_MAX_SEEN_FLOWS = 10000
_seq_counter = 0
_seq_lock = threading.Lock()
_regex_cache: dict[str, re.Pattern | None] = {}
_CLEANUP_INTERVAL_SECONDS = 60
_INTERCEPT_TTL_SECONDS = 120


def _next_seq() -> int:
    global _seq_counter
    with _seq_lock:
        _seq_counter += 1
        return _seq_counter


def _get_pattern(pat: str) -> re.Pattern | None:
    with _config_lock:
        if pat in _regex_cache:
            return _regex_cache[pat]
        # Compile inside the lock to prevent TOCTOU race with
        # _regex_cache.clear() in update_config.
        try:
            compiled = re.compile(pat)
        except re.error:
            compiled = None
        _regex_cache[pat] = compiled
        return compiled


_emit_error_logged = False


def _emit(event_type, data):
    global _emit_error_logged
    try:
        line = json.dumps({"event": event_type, "data": data}, ensure_ascii=True)
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
    except Exception as e:
        # Log the first failure to stderr so it can be diagnosed.
        if not _emit_error_logged:
            _emit_error_logged = True
            print(f"[addon_bridge] stdout emit failed: {e}", file=sys.stderr, flush=True)


def _safe_str(b):
    if b is None:
        return None
    if isinstance(b, str):
        return b
    return b.decode("utf-8", errors="replace")


def _safe_headers(headers):
    result = {}
    for k, v in headers.items():
        result[_safe_str(k) if isinstance(k, bytes) else k] = (
            _safe_str(v) if isinstance(v, bytes) else v
        )
    return result


def _parse_url(url: str):
    try:
        p = urlparse(url)
        host = p.netloc or p.hostname or ""
        path = p.path or "/"
        if p.query:
            path += "?" + p.query
        return host, path
    except Exception:
        return "", url


def _get_content_type(headers) -> str | None:
    ct = headers.get("content-type", "")
    if ct:
        return ct.split(";")[0].strip()
    return None


def _host_matches(host: str) -> bool:
    # Snapshot the list reference under the lock — the list itself is never
    # mutated in-place, only replaced atomically, so iterating the snapshot
    # is safe without holding the lock during iteration.
    with _config_lock:
        capture_hosts = _capture_hosts
    if not capture_hosts:
        return True
    host_lower = host.lower().split(":")[0]
    for pattern in capture_hosts:
        p = pattern.lower().strip()
        if not p:
            continue
        if host_lower == p or host_lower.endswith("." + p):
            return True
    return False


def _should_break(url: str, phase: str) -> bool:
    """Check if any enabled breakpoint rule matches the URL for the given phase."""
    with _config_lock:
        breakpoints = _breakpoints
    for rule in breakpoints:
        if not rule.get("enabled", True):
            continue
        pat = rule.get("url_pattern", "")
        if not pat:
            continue
        compiled = _get_pattern(pat)
        if compiled is None:
            matched = pat in url
        else:
            matched = bool(compiled.search(url))
        if not matched:
            continue
        if phase == "request" and rule.get("break_request", False):
            return True
        if phase == "response" and rule.get("break_response", False):
            return True
    return False


async def _get_client() -> httpx.AsyncClient:
    global _async_client
    if _async_client is None:
        # trust_env=False: the crypto endpoints are usually on an intranet and must not
        # be hijacked by an HTTP_PROXY set in the environment.
        _async_client = httpx.AsyncClient(timeout=5, trust_env=False)
    return _async_client


async def _http_post(url: str, content: bytes) -> str | None:
    if not url:
        return None
    try:
        client = await _get_client()
        resp = await client.post(url, content=content, headers={"Content-Type": "text/plain"})
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        _emit("log", {"level": "error", "msg": f"HTTP POST to {url} failed: {e}"})
        return None


# ── Local crypto ──
#
# Same algorithm as the learning tablet's CBB network SDK:
#   encrypt: plaintext → gzip → AES-128-ECB/PKCS7 → base64
#   decrypt: the reverse
#   AES key = SHA1(secret)[:16]
# `secret` is the raw key taken from the app binary, not the AES key itself.

_AES_BLOCK_BITS = 128


def _aes_key(secret: bytes) -> bytes:
    return hashlib.sha1(secret).digest()[:16]


def _local_encrypt(plaintext: str, secret: bytes) -> str:
    """Returns bare base64 cipher text."""
    # mtime=0 keeps the cipher text stable for the same plaintext (gzip writes the
    # current timestamp by default); the peer ignores the field when decompressing.
    compressed = gzip.compress(plaintext.encode("utf-8"), mtime=0)
    padder = PKCS7(_AES_BLOCK_BITS).padder()
    padded = padder.update(compressed) + padder.finalize()
    encryptor = Cipher(algorithms.AES(_aes_key(secret)), modes.ECB()).encryptor()
    cipher = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(cipher).decode("ascii")


def _local_decrypt(cipher_b64: str, secret: bytes) -> str:
    raw = base64.b64decode(cipher_b64)
    decryptor = Cipher(algorithms.AES(_aes_key(secret)), modes.ECB()).decryptor()
    padded = decryptor.update(raw) + decryptor.finalize()
    unpadder = PKCS7(_AES_BLOCK_BITS).unpadder()
    compressed = unpadder.update(padded) + unpadder.finalize()
    return gzip.decompress(compressed).decode("utf-8")


async def _decrypt(cipher_bytes):
    """Cipher body → plain body. With no backend configured the body is already plain."""
    if not cipher_bytes:
        return None
    try:
        cipher_str = cipher_bytes.decode("utf-8", errors="replace").strip().strip('"')
    except Exception:
        cipher_str = str(cipher_bytes)
    # Snapshot before await so the backend used is consistent with this call.
    with _config_lock:
        secret = _crypto_secret
        decrypt_url = _decrypt_url
    if secret is not None:
        try:
            return _local_decrypt(cipher_str, secret)
        except Exception as e:
            # Fall back to the raw text so the UI can still display something.
            _emit("log", {"level": "warn", "msg": f"local decrypt failed, fallback to raw body text: {e}"})
            return cipher_str
    if not decrypt_url:
        return cipher_str
    decrypted = await _http_post(decrypt_url, cipher_str.encode("utf-8"))
    if decrypted is None:
        _emit("log", {"level": "warn", "msg": "decrypt API failed, fallback to raw body text"})
        return cipher_str
    return decrypted


async def _encrypt_body(plaintext, original: bytes | None = None):
    """Plain body → cipher body, or None when the configured backend failed.

    Whether the cipher text is wrapped in quotes follows `original` — the body being
    replaced. Both shapes occur on the wire (bare base64 and a quoted JSON string), so
    hard-coding either one breaks the other.
    """
    quoted = bool(original) and original.lstrip().startswith(b'"')
    with _config_lock:
        secret = _crypto_secret
        encrypt_url = _encrypt_url
    if secret is not None:
        try:
            cipher = _local_encrypt(plaintext, secret)
        except Exception as e:
            _emit("log", {"level": "error", "msg": f"local encrypt failed: {e}"})
            return None
        return (f'"{cipher}"' if quoted else cipher).encode("utf-8")
    if not encrypt_url:
        # No crypto backend at all — this endpoint speaks plain text, send it as-is.
        return plaintext.encode("utf-8")
    result = await _http_post(encrypt_url, plaintext.encode("utf-8"))
    if result is None:
        _emit("log", {"level": "error", "msg": "encrypt API call failed, returning None"})
        return None
    # The encrypt API returns JSON like {"data": "...cipher..."}, extract the cipher text
    try:
        obj = json.loads(result)
        if isinstance(obj, dict) and "data" in obj:
            return (f'"{obj["data"]}"' if quoted else str(obj["data"])).encode("utf-8")
    except (json.JSONDecodeError, TypeError):
        pass
    # Fallback: use raw response as-is
    return result.encode("utf-8")


# ── Mock rules ──


def merge_patch(target, patch):
    """RFC 7386 JSON Merge Patch: merge `patch` into `target`.

    A None value deletes the field; arrays are replaced wholesale, individual
    elements inside an array cannot be addressed.
    """
    if not isinstance(patch, dict):
        return patch
    base = dict(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            base.pop(key, None)
        else:
            base[key] = merge_patch(base.get(key), value)
    return base


def _rule_fingerprint(rule: dict) -> str:
    """Content hash of a rule. Hit counts are kept per fingerprint, so a config
    reload only resets the counter of the rules that actually changed."""
    canonical = json.dumps(rule, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()


def _take_mock_rule(url: str, method: str) -> dict | None:
    """Return the first matching rule that still has hits left, counting the hit."""
    with _config_lock:
        rules = _mock_rules
    with _mock_lock:
        for rule in rules:
            if not rule.get("enabled", True):
                continue
            pat = rule.get("url_pattern") or ""
            if not pat:
                continue
            compiled = _get_pattern(pat)
            matched = bool(compiled.search(url)) if compiled is not None else pat in url
            if not matched:
                continue
            want_method = (rule.get("method") or "").strip().upper()
            if want_method and want_method != method.upper():
                continue
            times = int(rule.get("times") or 0)
            fingerprint = _rule_fingerprint(rule)
            if times and _mock_used.get(fingerprint, 0) >= times:
                continue
            _mock_used[fingerprint] = _mock_used.get(fingerprint, 0) + 1
            return rule
    return None


async def _rule_delay(rule: dict) -> None:
    delay_ms = int(rule.get("delay_ms") or 0)
    if delay_ms > 0:
        await asyncio.sleep(delay_ms / 1000)


def _looks_like_json(text: str) -> bool:
    stripped = (text or "").lstrip()
    return stripped.startswith("{") or stripped.startswith("[")


# ── WebSocket replay ──


def _arm_ws_replay(path: str, frames: list[dict]) -> None:
    with _ws_lock:
        if frames:
            _ws_replays[path] = frames
        else:
            _ws_replays.pop(path, None)


def _take_ws_replay(path: str) -> list[dict] | None:
    """A replay is consumed by the first connection that handshakes on its path."""
    with _ws_lock:
        return _ws_replays.pop(path, None)


async def _do_intercept(flow: http.HTTPFlow, intercept_id: str, phase: str,
                        url: str, method: str, body_plain: str | None,
                        status_code: int | None = None):
    """
    Intercept a flow using mitmproxy's native mechanism.
    """
    waiter = asyncio.Event()
    with _intercept_lock:
        _intercepted_flows[intercept_id] = (flow, phase)
        _intercept_waiters[intercept_id] = waiter
        early_result = _intercept_results.get(intercept_id)

    _emit("log", {"level": "info", "msg": f"[intercept] ENTER intercept_id={intercept_id} phase={phase} url={url}"})
    _emit("log", {"level": "debug", "msg": f"[intercept] flow.id={flow.id} flow.intercepted={flow.intercepted} flow.live={flow.live}"})

    # Important ordering: ensure flow is intercepted before notifying frontend.
    # Otherwise a fast "pass/abort" can race with flow.intercept() and be lost.
    _emit("log", {"level": "debug", "msg": f"[intercept] calling flow.intercept() for {intercept_id}"})
    flow.intercept()
    _emit("log", {"level": "debug", "msg": f"[intercept] flow.intercepted={flow.intercepted} _resume_event={flow._resume_event}"})

    _emit("intercept", {
        "flow_id": intercept_id, "phase": phase,
        "method": method, "url": url,
        "body_plain": body_plain, "status_code": status_code,
    })

    # If frontend response arrived before this coroutine started waiting, resume now.
    if early_result is not None:
        _emit("log", {"level": "info", "msg": f"[intercept] early result exists for {intercept_id}, resume immediately"})
        waiter.set()

    try:
        await asyncio.wait_for(waiter.wait(), timeout=120)
    except asyncio.TimeoutError:
        _emit("log", {"level": "warn", "msg": f"[intercept] timeout waiting for frontend decision: {intercept_id}, auto-pass"})

    _emit("log", {"level": "info", "msg": f"[intercept] wait_for_resume() returned for {intercept_id}"})

    # Flow has been resumed — retrieve the result
    with _intercept_lock:
        _intercepted_flows.pop(intercept_id, None)
        _intercept_waiters.pop(intercept_id, None)
        _intercept_result_times.pop(intercept_id, None)
        result = _intercept_results.pop(intercept_id, None)

    # Explicitly resume from the flow loop after decision/timeout.
    if flow.intercepted:
        flow.resume()

    _emit("log", {"level": "debug", "msg": f"[intercept] result for {intercept_id}: {result[0] if result else 'None'}"})
    return result


async def _sweep_orphan_intercept_results() -> None:
    while True:
        await asyncio.sleep(_CLEANUP_INTERVAL_SECONDS)
        cutoff = time.time() - _INTERCEPT_TTL_SECONDS
        removed = 0
        with _intercept_lock:
            expired_ids = [
                flow_id
                for flow_id, created_at in _intercept_result_times.items()
                if created_at < cutoff and flow_id not in _intercepted_flows
            ]
            for flow_id in expired_ids:
                _intercept_result_times.pop(flow_id, None)
                if _intercept_results.pop(flow_id, None) is not None:
                    removed += 1
        if removed:
            _emit("log", {"level": "debug", "msg": f"cleaned {removed} stale intercept result(s)"})


# mitmproxy 的报错末尾常跟一句命令行用法提示，例如
#   Try specifying a different port by using `--mode regular@8082`.
# 这个应用没有命令行入口，照搬只会把人引向一个不存在的开关。
_CLI_HINT_LINE = re.compile(r"^.*--mode\s.*$\n?", re.MULTILINE)
_PORT_IN_USE_HINT = "请在设置里换一个代理端口，或先关掉占用该端口的程序。"


def _rewrite_mitmproxy_message(message: str) -> str:
    """把 mitmproxy 的报错改写成界面上说得通的话。

    只做两件事：摘掉命令行用法提示，以及给端口占用补一句可执行的处置。认不出的报错
    原样透传——宁可多余，也不要把线索吞掉。
    """
    cleaned = _CLI_HINT_LINE.sub("", message).strip()
    if "address already in use" in cleaned.lower():
        cleaned += "\n" + _PORT_IN_USE_HINT
    return cleaned


class _MitmproxyLogForwarder(logging.Handler):
    """Forward mitmproxy's own log records to the Rust host.

    mitmproxy logs through the standard logging module, and with with_termlog=False
    nothing else consumes those records. Without this the reason a startup fails —
    a port already in use, say — is discarded, and all that reaches the user is
    mitmproxy's bare "Error logged during startup, exiting..." on stderr.

    (There is an `add_log` addon hook as well, but it only fires when the legacy
    log-events addon is loaded, which DumpMaster does not load by default.)
    """

    _LEVELS = {"CRITICAL": "error", "ERROR": "error", "WARNING": "warn"}

    def emit(self, record: logging.LogRecord) -> None:
        level = self._LEVELS.get(record.levelname)
        if level is None:
            return
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        _emit("log", {"level": level, "msg": f"[mitmproxy] {_rewrite_mitmproxy_message(message)}"})


class SniffAddon:
    def __init__(self):
        self._flow_seq: OrderedDict[str, int] = OrderedDict()
        self._flow_start: dict[str, float] = {}
        # Cache decrypted request body so the response phase doesn't call
        # the decrypt API a second time for the same flow.
        self._flow_req_plain: dict[str, str | None] = {}
        # Mock rule taken in the request phase, read again in the response phase.
        self._flow_rule: dict[str, dict] = {}
        # Response plaintext already known (mocked, patched or edited at a breakpoint),
        # so the record does not decrypt the same body twice.
        self._flow_resp_plain: dict[str, str | None] = {}
        # "device" or "outbound", per flow.
        self._flow_origin: dict[str, str] = {}
        # WebSocket connection state, keyed by flow id.
        self._ws: dict[str, dict] = {}
        self._ws_conn = 0

    def running(self):
        """Called by mitmproxy after it has successfully bound the port and is ready.
        This is the authoritative signal that the proxy is actually running.
        """
        _emit("status", {"running": True})
        _emit("log", {"level": "info", "msg": "mitmproxy is running and ready"})

    def _get_seq(self, flow_id: str) -> int:
        if flow_id in self._flow_seq:
            return self._flow_seq[flow_id]
        seq = _next_seq()
        self._flow_seq[flow_id] = seq
        while len(self._flow_seq) > _MAX_SEEN_FLOWS:
            old_id, _ = self._flow_seq.popitem(last=False)
            self._flow_start.pop(old_id, None)
            self._flow_req_plain.pop(old_id, None)
            self._flow_rule.pop(old_id, None)
            self._flow_origin.pop(old_id, None)
            self._flow_resp_plain.pop(old_id, None)
        return seq

    def _origin(self, flow: http.HTTPFlow) -> str:
        """Outbound requests self-identify with a header; strip it before forwarding."""
        if flow.request.headers.get(ORIGIN_HEADER) == ORIGIN_OUTBOUND:
            del flow.request.headers[ORIGIN_HEADER]
            self._flow_origin[flow.id] = "outbound"
            return "outbound"
        return self._flow_origin.get(flow.id, "device")

    async def request(self, flow: http.HTTPFlow):
        try:
            url = flow.request.pretty_url
            host, path = _parse_url(url)
            if not _host_matches(host):
                return

            origin = self._origin(flow)
            seq = self._get_seq(flow.id)
            start_ms = time.time() * 1000
            self._flow_start[flow.id] = start_ms

            req_body = flow.request.content or b""
            req_plain = await _decrypt(req_body) if req_body else None
            # Cache so the response handler can reuse it without a second decrypt call.
            self._flow_req_plain[flow.id] = req_plain

            _emit("record", {
                "flow_id": flow.id, "seq": seq, "method": flow.request.method,
                "url": url, "host": host, "path": path, "origin": origin,
                "request_content_type": _get_content_type(flow.request.headers),
                "request_headers": _safe_headers(flow.request.headers),
                "request_plain": req_plain, "request_size": len(req_body),
                "response_status": None, "response_content_type": None,
                "response_headers": None, "response_plain": None, "response_size": None,
                "start_time": start_ms, "duration": None, "status": "pending",
                "mock": None,
            })

            # Mock rules win over breakpoints: a rule already says what to answer,
            # so there is nothing left to decide.
            rule = _take_mock_rule(url, flow.request.method)
            if rule is not None:
                self._flow_rule[flow.id] = rule
                if rule.get("mode") == "respond":
                    await self._serve_respond(flow, rule)
                return

            # Outbound requests skip breakpoints: they are fired by this app itself and
            # pausing them would only stall the caller that is waiting for the response.
            if origin == "outbound":
                return

            # Breakpoint check for request phase
            if _should_break(url, "request"):
                intercept_id = flow.id + ":req"
                result = await _do_intercept(
                    flow, intercept_id, "request", url,
                    flow.request.method, req_plain,
                )
                if result:
                    action, new_body = result
                    if action == "abort":
                        flow.kill()
                        return
                    elif action == "pass" and new_body is not None:
                        if new_body != req_plain:
                            encrypted = await _encrypt_body(new_body, req_body)
                            if encrypted is not None:
                                flow.request.content = encrypted
                                self._flow_req_plain[flow.id] = new_body
                            else:
                                _emit("log", {"level": "error",
                                       "msg": "encrypt failed for request, keeping original"})
        except Exception as e:
            _emit("log", {"level": "error", "msg": f"request handler error: {e}"})

    async def _serve_respond(self, flow: http.HTTPFlow, rule: dict):
        """Answer straight from the rule without contacting the server."""
        body_plain = rule.get("body") or ""
        if rule.get("encrypt", True):
            content = await _encrypt_body(body_plain)
            if content is None:
                self._fail_closed(flow, f"mock rule {rule.get('name')!r}: 响应体加密失败")
                return
        else:
            content = body_plain.encode("utf-8")

        await _rule_delay(rule)
        # Describe what actually goes on the wire: an encrypted body is no longer JSON.
        is_json = not rule.get("encrypt", True) and _looks_like_json(body_plain)
        content_type = "application/json; charset=utf-8" if is_json else "text/plain; charset=utf-8"
        flow.response = http.Response.make(
            int(rule.get("status") or 200), content, {"Content-Type": content_type}
        )
        self._flow_resp_plain[flow.id] = body_plain

    async def _apply_patch(self, flow: http.HTTPFlow, rule: dict) -> str | None:
        """Merge the rule's JSON fragment into the real response body."""
        original = flow.response.content or b""
        plain = await _decrypt(original) if original else ""
        try:
            document = json.loads(plain) if plain and plain.strip() else {}
            fragment = json.loads(rule.get("body") or "{}")
        except json.JSONDecodeError as e:
            self._fail_closed(flow, f"mock rule {rule.get('name')!r}: 响应体或改写片段不是 JSON：{e}")
            return None

        text = json.dumps(merge_patch(document, fragment), ensure_ascii=False)
        if rule.get("encrypt", True):
            content = await _encrypt_body(text, original)
            if content is None:
                self._fail_closed(flow, f"mock rule {rule.get('name')!r}: 改写后的响应体加密失败")
                return None
        else:
            content = text.encode("utf-8")

        await _rule_delay(rule)
        flow.response.content = content
        return text

    def _fail_closed(self, flow: http.HTTPFlow, message: str):
        """Abort with 502 rather than let a half-applied intervention through."""
        _emit("log", {"level": "error", "msg": message})
        flow.response = http.Response.make(
            502, f"xxj-sniff: {message}".encode("utf-8"),
            {"Content-Type": "text/plain; charset=utf-8"},
        )
        self._flow_resp_plain[flow.id] = f"xxj-sniff: {message}"

    async def response(self, flow: http.HTTPFlow):
        try:
            url = flow.request.pretty_url
            host, path = _parse_url(url)
            if not _host_matches(host):
                return

            origin = self._flow_origin.get(flow.id, "device")
            seq = self._get_seq(flow.id)
            start_ms = self._flow_start.get(flow.id, time.time() * 1000)

            rule = self._flow_rule.pop(flow.id, None)
            mock_info = None
            if rule is not None:
                mock_info = {"rule": rule.get("name") or "", "mode": rule.get("mode") or ""}
                if rule.get("mode") == "patch" and flow.response is not None:
                    patched = await self._apply_patch(flow, rule)
                    if patched is not None:
                        self._flow_resp_plain[flow.id] = patched

            # Breakpoint check for response phase. Skipped for mocked and outbound flows.
            if rule is None and origin != "outbound" and _should_break(url, "response"):
                resp_body = flow.response.content or b""
                resp_plain = await _decrypt(resp_body) if resp_body else None
                intercept_id = flow.id + ":resp"
                result = await _do_intercept(
                    flow, intercept_id, "response", url,
                    flow.request.method, resp_plain,
                    status_code=flow.response.status_code,
                )
                if result:
                    action, new_body = result
                    if action == "abort":
                        flow.kill()
                        return
                    elif action == "pass" and new_body is not None:
                        if new_body != resp_plain:
                            encrypted = await _encrypt_body(new_body, resp_body)
                            if encrypted is not None:
                                flow.response.content = encrypted
                                resp_plain = new_body
                            else:
                                _emit("log", {"level": "error",
                                       "msg": "encrypt failed for response, keeping original"})
                self._flow_resp_plain[flow.id] = resp_plain

            # Recalculate duration after potential intercept wait
            duration = time.time() * 1000 - start_ms

            req_body = flow.request.content or b""
            # Reuse the cached decrypted request body from the request phase
            # to avoid a redundant decrypt API call.
            req_plain = self._flow_req_plain.pop(flow.id, None)
            resp_body = flow.response.content or b""
            if flow.id in self._flow_resp_plain:
                resp_plain = self._flow_resp_plain.pop(flow.id)
            else:
                resp_plain = await _decrypt(resp_body) if resp_body else None

            _emit("record", {
                "flow_id": flow.id, "seq": seq, "method": flow.request.method,
                "url": url, "host": host, "path": path, "origin": origin,
                "request_content_type": _get_content_type(flow.request.headers),
                "request_headers": _safe_headers(flow.request.headers),
                "request_plain": req_plain, "request_size": len(req_body),
                "response_status": flow.response.status_code,
                "response_content_type": _get_content_type(flow.response.headers),
                "response_headers": _safe_headers(flow.response.headers),
                "response_plain": resp_plain, "response_size": len(resp_body),
                "start_time": start_ms, "duration": round(duration, 1),
                "status": "complete", "mock": mock_info,
            })
            self._flow_start.pop(flow.id, None)
            self._flow_origin.pop(flow.id, None)
        except Exception as e:
            _emit("log", {"level": "error", "msg": f"response handler error: {e}"})

    async def error(self, flow: http.HTTPFlow):
        try:
            url = flow.request.pretty_url
            host, path = _parse_url(url)
            if not _host_matches(host):
                return
            seq = self._get_seq(flow.id)
            start_ms = self._flow_start.pop(flow.id, time.time() * 1000)
            duration = time.time() * 1000 - start_ms
            origin = self._flow_origin.pop(flow.id, "device")
            self._flow_rule.pop(flow.id, None)
            self._flow_resp_plain.pop(flow.id, None)
            req_body = flow.request.content or b""
            # Use cached decrypted request body if available.
            req_plain = self._flow_req_plain.pop(flow.id, None)
            if req_plain is None and req_body:
                req_plain = await _decrypt(req_body)
            error_msg = str(flow.error) if flow.error else "Unknown error"
            _emit("record", {
                "flow_id": flow.id, "seq": seq, "method": flow.request.method,
                "url": url, "host": host, "path": path, "origin": origin,
                "request_content_type": _get_content_type(flow.request.headers),
                "request_headers": _safe_headers(flow.request.headers),
                "request_plain": req_plain, "request_size": len(req_body),
                "response_status": None, "response_content_type": None,
                "response_headers": None, "response_plain": error_msg,
                "response_size": None,
                "start_time": start_ms, "duration": round(duration, 1),
                "status": "error", "mock": None,
            })
        except Exception as e:
            _emit("log", {"level": "error", "msg": f"error handler error: {e}"})

    # ── WebSocket ──
    #
    # Frames are recorded, never intervened on: they are forwarded as-is and do not go
    # through the crypto backend, which serves the business gateway rather than the WS
    # protocol. A connection with a replay armed is the exception — see _run_replay.

    def websocket_start(self, flow: http.HTTPFlow):
        try:
            url = flow.request.pretty_url
            host, _ = _parse_url(url)
            if not _host_matches(host):
                return
            self._ws_conn += 1
            # The replay is keyed on the bare path, without the query string, so that a
            # per-connection token in the query does not stop it from matching.
            path = urlparse(url).path
            replay = _take_ws_replay(path)
            started = time.time()
            self._ws[flow.id] = {
                "conn": self._ws_conn,
                "url": url,
                "host": host,
                # Same epoch-seconds clock as the frame timestamps, so the difference
                # is a meaningful offset.
                "start": started,
                "frames": 0,
                "replay": replay,
                "replay_task": None,
            }
            _emit("ws_conn", {
                "conn": self._ws_conn, "url": url, "host": host, "path": path,
                "state": "open", "frames": 0, "replaying": replay is not None,
                "start_time": started * 1000,
            })
        except Exception as e:
            _emit("log", {"level": "error", "msg": f"websocket_start error: {e}"})

    def websocket_message(self, flow: http.HTTPFlow):
        try:
            state = self._ws.get(flow.id)
            if state is None or flow.websocket is None or not flow.websocket.messages:
                return
            message = flow.websocket.messages[-1]
            if state["replay"] is not None:
                self._on_replay_message(flow, state, message)
            state["frames"] += 1
            content = message.content or b""
            _emit("ws_frame", {
                "conn": state["conn"],
                "seq": state["frames"],
                "url": state["url"],
                "host": state["host"],
                "t_ms": round((message.timestamp - state["start"]) * 1000, 1),
                "dir": "up" if message.from_client else "down",
                "type": "text" if message.is_text else "binary",
                "size": len(content),
                "injected": bool(message.injected),
                "payload": (
                    content.decode("utf-8", errors="replace")
                    if message.is_text
                    else base64.b64encode(content).decode("ascii")
                ),
            })
        except Exception as e:
            _emit("log", {"level": "error", "msg": f"websocket_message error: {e}"})

    def websocket_end(self, flow: http.HTTPFlow):
        state = self._ws.pop(flow.id, None)
        if state is None:
            return
        task = state.get("replay_task")
        if task is not None and not task.done():
            task.cancel()
        _emit("ws_conn", {
            "conn": state["conn"], "url": state["url"], "host": state["host"],
            "path": urlparse(state["url"]).path, "state": "closed",
            "frames": state["frames"], "replaying": state["replay"] is not None,
            "start_time": state["start"] * 1000,
        })

    def _on_replay_message(self, flow: http.HTTPFlow, state: dict, message):
        """During a replay every real frame is dropped, in both directions.

        Upstream frames are withheld from the server, and whatever the server pushes on
        its own is withheld from the client — a replay stands in for the server's answer,
        so letting real downstream frames interleave with the injected ones would corrupt
        it. The first upstream frame starts the replay clock. Injected frames pass.
        """
        if message.injected:
            return
        if message.from_client and state["replay_task"] is None:
            state["replay_task"] = asyncio.create_task(self._run_replay(flow, state))
        message.drop()

    async def _run_replay(self, flow: http.HTTPFlow, state: dict):
        """Inject the recorded downstream frames back at their original relative times."""
        frames: list[dict] = state["replay"]
        down = [f for f in frames if f.get("dir") == "down"]
        # Align to the first upstream frame of the recording: a replay also starts
        # counting from the moment an upstream frame arrives. With no upstream frame
        # recorded, fall back to the first frame, i.e. align to the handshake.
        anchor = next(
            (f.get("t_ms", 0.0) for f in frames if f.get("dir") == "up"),
            frames[0].get("t_ms", 0.0) if frames else 0.0,
        )
        started = time.monotonic()
        sent = 0
        try:
            for frame in down:
                delay = (frame.get("t_ms", 0.0) - anchor) / 1000 - (time.monotonic() - started)
                if delay > 0:
                    await asyncio.sleep(delay)
                is_text = frame.get("type") == "text"
                payload = frame.get("payload") or ""
                content = payload.encode("utf-8") if is_text else base64.b64decode(payload)
                _master.commands.call("inject.websocket", flow, True, content, is_text)
                sent += 1
        except asyncio.CancelledError:
            _emit("log", {"level": "info",
                   "msg": f"WS #{state['conn']} replay cancelled after {sent}/{len(down)} frame(s)"})
            raise
        except Exception as e:
            _emit("log", {"level": "error",
                   "msg": f"WS #{state['conn']} replay failed after {sent}/{len(down)} frame(s): {e}"})
        else:
            _emit("log", {"level": "info",
                   "msg": f"WS #{state['conn']} replay done, {sent} frame(s) injected"})


def _handle_intercept_respond(line: str):
    """Handle the TSV intercept_respond protocol line from Rust."""
    parts = line.split("\t", 3)
    if len(parts) < 3:
        _emit("log", {"level": "warn", "msg": f"stdin line protocol malformed: {line[:120]}"})
        return
    flow_id = parts[1]
    action = parts[2] or "pass"
    body_b64 = parts[3] if len(parts) > 3 else ""
    body = None
    if body_b64:
        try:
            body = base64.b64decode(body_b64.encode("ascii")).decode("utf-8", errors="replace")
        except Exception as decode_err:
            _emit("log", {"level": "error", "msg": f"stdin body base64 decode failed for {flow_id}: {decode_err}"})

    _emit("log", {"level": "info", "msg": f"[stdin] intercept_respond flow_id={flow_id} action={action} body_len={len(body) if body else 0}"})
    with _intercept_lock:
        _intercept_results[flow_id] = (action, body)
        _intercept_result_times[flow_id] = time.time()
        entry = _intercepted_flows.get(flow_id)
        waiter = _intercept_waiters.get(flow_id)
        active_ids = list(_intercepted_flows.keys())
    _emit("log", {"level": "info", "msg": f"[stdin] active_intercepts={active_ids}"})
    if waiter and _loop:
        # Wake the _do_intercept coroutine which will handle flow.resume().
        # Do NOT call flow.resume() directly here to avoid double-resume.
        _loop.call_soon_threadsafe(waiter.set)
        _emit("log", {"level": "info", "msg": f"[stdin] waiter set for {flow_id}"})
    elif entry:
        _emit("log", {"level": "warn", "msg": f"[stdin] flow entry exists for {flow_id} but no waiter, kept as pending result"})
    else:
        _emit("log", {"level": "warn", "msg": f"[stdin] no live entry for flow_id={flow_id}, kept as pending result"})


async def _send_outbound(cmd: dict) -> None:
    """Send a request as if it came from the device, routed back through our own proxy.

    Going through the proxy means the request is recorded and matched against mock
    rules exactly like device traffic; the record marks it with origin "outbound".
    """
    request_id = str(cmd.get("id") or "")
    url = str(cmd.get("url") or "")
    method = (str(cmd.get("method") or "POST")).upper()
    headers = {str(k): str(v) for k, v in (cmd.get("headers") or {}).items()}
    body_plain = cmd.get("body_plain")
    encrypt = bool(cmd.get("encrypt", True))

    def fail(message: str) -> None:
        _emit("log", {"level": "error", "msg": f"[outbound] {message}"})
        _emit("outbound_result", {"id": request_id, "url": url, "method": method,
                                  "error": message})

    if not url:
        fail("URL 为空")
        return

    content = None
    if body_plain:
        if encrypt:
            content = await _encrypt_body(body_plain)
            if content is None:
                fail("请求体加密失败")
                return
        else:
            content = body_plain.encode("utf-8")

    headers[ORIGIN_HEADER] = ORIGIN_OUTBOUND
    with _config_lock:
        port = _proxy_port
    proxy = f"http://127.0.0.1:{port}"
    try:
        # verify=False: this request goes through our own mitmproxy and will always hit
        # its self-signed certificate. trust_env=False: don't let an ambient HTTP_PROXY
        # steal it away from our proxy.
        async with httpx.AsyncClient(proxy=proxy, verify=False, trust_env=False, timeout=30) as client:
            resp = await client.request(method, url, content=content, headers=headers)
    except Exception as e:
        fail(f"请求发送失败：{e}")
        return

    # A failed response decrypt is not fatal here: gateway error pages (nginx 502,
    # rate-limit pages) are plain text by nature, and they are exactly what an
    # outbound probe needs to see.
    response_plain = await _decrypt(resp.content) if (resp.content and encrypt) else resp.text
    _emit("outbound_result", {
        "id": request_id, "url": url, "method": method,
        "encrypted": encrypt,
        "request_plain": body_plain,
        "response_status": resp.status_code,
        "response_plain": response_plain,
        "error": None,
    })


def _apply_config_update(cmd: dict) -> None:
    """Apply an update_config command. Runs on the stdin thread."""
    global _encrypt_url, _decrypt_url, _crypto_secret, _capture_hosts, _breakpoints, _mock_rules
    with _config_lock:
        url_changed = False
        if "encrypt_url" in cmd:
            url_changed = url_changed or (_encrypt_url != cmd["encrypt_url"])
            _encrypt_url = cmd["encrypt_url"]
        if "decrypt_url" in cmd:
            url_changed = url_changed or (_decrypt_url != cmd["decrypt_url"])
            _decrypt_url = cmd["decrypt_url"]
        if "crypto_secret" in cmd:
            _crypto_secret = _parse_secret(cmd.get("crypto_secret"), cmd.get("crypto_secret_b64"))
        if "capture_hosts" in cmd:
            _capture_hosts = [h for h in cmd["capture_hosts"] if h.strip()]
        if "breakpoints" in cmd:
            _breakpoints = cmd["breakpoints"]
            _regex_cache.clear()
        if "mock_rules" in cmd:
            _mock_rules = cmd["mock_rules"]
            _regex_cache.clear()
        bp_count = len(_breakpoints)
        rules = _mock_rules
        hosts_snapshot = _capture_hosts
        local_crypto = _crypto_secret is not None

    # Outside _config_lock: _take_mock_rule holds _mock_lock while taking _config_lock,
    # so acquiring them in the other order here would risk a deadlock.
    keep = {_rule_fingerprint(r) for r in rules}
    with _mock_lock:
        for fingerprint in [k for k in _mock_used if k not in keep]:
            del _mock_used[fingerprint]

    # Rebuild httpx client when encrypt/decrypt URLs change so
    # the connection pool targets the correct host.
    if url_changed and _async_client is not None and _loop:
        async def _rebuild_client():
            global _async_client
            try:
                await _async_client.aclose()
            except Exception:
                pass
            _async_client = None
        _loop.call_soon_threadsafe(lambda: _loop.create_task(_rebuild_client()))

    _emit("log", {"level": "info",
           "msg": f"config updated: {bp_count} breakpoint(s), {len(rules)} mock rule(s), "
                  f"crypto={'local' if local_crypto else 'http'}, hosts={hosts_snapshot}"})


def _stdin_reader():
    """Read commands from stdin (runs in a separate thread).

    Two protocols are supported:
      1. TSV  — intercept_respond<TAB>flow_id<TAB>action<TAB>base64_body
      2. JSON — {"command": "update_config" | "ws_replay" | "outbound", ...}

    The TSV prefix is checked first so that a TSV line is never accidentally
    fed into the JSON parser (which would produce a misleading error).
    """
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue

            # TSV protocol takes priority — check prefix before attempting JSON.
            if line.startswith("intercept_respond\t"):
                _handle_intercept_respond(line)
                continue

            try:
                cmd = json.loads(line)
                cmd_type = cmd.get("command", "")

                if cmd_type == "update_config":
                    _apply_config_update(cmd)
                elif cmd_type == "ws_replay":
                    frames = cmd.get("frames") or []
                    path = str(cmd.get("path") or "")
                    _arm_ws_replay(path, frames)
                    _emit("log", {"level": "info",
                           "msg": f"ws replay {'armed' if frames else 'cleared'} for {path}"
                                  f" ({len(frames)} frame(s))"})
                elif cmd_type == "outbound":
                    if _loop is None:
                        _emit("outbound_result", {"id": cmd.get("id"),
                               "error": "代理未运行，无法代发请求"})
                    else:
                        _loop.call_soon_threadsafe(
                            lambda c=cmd: _loop.create_task(_send_outbound(c))
                        )

            except json.JSONDecodeError as e:
                _emit("log", {"level": "warn", "msg": f"stdin json decode failed: {e}"})
                continue
            except Exception as e:
                _emit("log", {"level": "error", "msg": f"stdin command handling failed: {e}"})
                continue
    except Exception as e:
        _emit("log", {"level": "error", "msg": f"stdin reader crashed: {e}"})


def _parse_secret(secret: object, is_b64: object) -> bytes | None:
    """Parse the local crypto secret. Empty means "no local backend"."""
    text = str(secret or "").strip()
    if not text:
        return None
    if is_b64:
        try:
            return base64.b64decode(text)
        except Exception as e:
            _emit("log", {"level": "error", "msg": f"crypto_secret base64 decode failed: {e}"})
            return None
    return text.encode("utf-8")


def main():
    global _master, _encrypt_url, _decrypt_url, _capture_hosts, _breakpoints
    global _mock_rules, _proxy_port, _loop

    # Parse config from --config JSON arg
    config = {}
    args = sys.argv[1:]
    if "--selftest" in args:
        _selftest()
        return
    if "--config" in args:
        idx = args.index("--config")
        if idx + 1 < len(args):
            try:
                config = json.loads(args[idx + 1])
            except json.JSONDecodeError:
                _emit("log", {"level": "error", "msg": "Failed to parse --config JSON"})
                sys.exit(1)

    port = config.get("proxy_port", 8080)
    _proxy_port = port
    _encrypt_url = config.get("encrypt_url", "")
    _decrypt_url = config.get("decrypt_url", "")
    _capture_hosts = [h for h in config.get("capture_hosts", []) if h.strip()]
    _breakpoints = config.get("breakpoints", [])
    _mock_rules = config.get("mock_rules", [])

    _emit("log", {"level": "info",
           "msg": f"starting mitmproxy on port {port}, "
                  f"{len(_breakpoints)} breakpoint(s), {len(_mock_rules)} mock rule(s), "
                  f"hosts={_capture_hosts}"})

    # 装在根 logger 上：绑定端口失败是 mitmproxy 的 proxyserver 记的，不在我们的 logger 下
    root_logger = logging.getLogger()
    root_logger.addHandler(_MitmproxyLogForwarder(level=logging.WARNING))
    if root_logger.level > logging.WARNING or root_logger.level == logging.NOTSET:
        root_logger.setLevel(logging.WARNING)

    stdin_thread = threading.Thread(target=_stdin_reader, daemon=True)
    stdin_thread.start()

    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    cleanup_task: asyncio.Task | None = None

    async def _start():
        global _master
        opts = Options(listen_host="127.0.0.1", listen_port=port)
        _master = DumpMaster(opts, with_termlog=False, with_dumper=False)
        _master.addons.add(SniffAddon())
        # Do NOT emit "running: True" here.
        # SniffAddon.running() fires after mitmproxy successfully binds the port.
        await _master.run()

    try:
        cleanup_task = _loop.create_task(_sweep_orphan_intercept_results())
        _loop.run_until_complete(_start())
    except KeyboardInterrupt:
        pass
    except SystemExit:
        pass
    except Exception as e:
        _emit("log", {"level": "error", "msg": f"proxy error: {e}"})
    finally:
        if cleanup_task is not None:
            cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                _loop.run_until_complete(cleanup_task)
        if _async_client:
            try:
                _loop.run_until_complete(_async_client.aclose())
            except Exception:
                pass
        _emit("status", {"running": False})


def _selftest() -> None:
    """Runnable check for the logic that has no other coverage: local crypto,
    JSON merge patch and mock rule matching. Run with `--selftest`."""
    global _mock_rules
    secret = b"selftest-secret"
    plain = '{"a": 1, "\u4e2d\u6587": "x"}'
    assert _local_decrypt(_local_encrypt(plain, secret), secret) == plain
    # The derived AES key is SHA1(secret)[:16], not the secret itself.
    assert len(_aes_key(secret)) == 16

    assert merge_patch({"a": 1, "b": {"c": 2}}, {"b": {"c": 3}}) == {"a": 1, "b": {"c": 3}}
    assert merge_patch({"a": 1, "b": 2}, {"b": None}) == {"a": 1}
    assert merge_patch({"a": [1, 2]}, {"a": [3]}) == {"a": [3]}

    _mock_rules = [
        {"name": "once", "url_pattern": r"/api/v1/foo", "method": "POST",
         "mode": "respond", "times": 1, "enabled": True},
        {"name": "always", "url_pattern": r"/api/v1/", "mode": "patch",
         "times": 0, "enabled": True},
        {"name": "off", "url_pattern": r"/api/v1/", "mode": "patch", "enabled": False},
    ]
    _mock_used.clear()
    assert _take_mock_rule("http://h/api/v1/foo", "POST")["name"] == "once"
    # times: 1 is spent, so the next hit falls through to the unlimited rule.
    assert _take_mock_rule("http://h/api/v1/foo", "POST")["name"] == "always"
    # A method mismatch skips the rule even on the first hit.
    _mock_used.clear()
    assert _take_mock_rule("http://h/api/v1/foo", "GET")["name"] == "always"
    assert _take_mock_rule("http://h/other", "GET") is None

    bind_error = (
        "[Errno 98] HTTP(S) proxy failed to listen on 127.0.0.1:8080 with [Errno 98] "
        "error while attempting to bind on address ('127.0.0.1', 8080): "
        "[errno 98] address already in use\n"
        "Try specifying a different port by using `--mode regular@8082`."
    )
    rewritten = _rewrite_mitmproxy_message(bind_error)
    assert "--mode" not in rewritten, rewritten
    assert "address already in use" in rewritten, rewritten
    assert rewritten.endswith(_PORT_IN_USE_HINT), rewritten
    # 认不出的报错原样透传，不能被吞掉
    assert _rewrite_mitmproxy_message("Client TLS handshake failed") == "Client TLS handshake failed"

    assert _parse_secret("", False) is None
    assert _parse_secret(base64.b64encode(secret).decode(), True) == secret
    assert _parse_secret("abc", False) == b"abc"

    print("selftest passed")


if __name__ == "__main__":
    main()
