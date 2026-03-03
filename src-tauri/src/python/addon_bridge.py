"""
Standalone Python script for running mitmproxy as a subprocess.
Events are emitted as JSON lines to stdout.
Commands are received as JSON lines on stdin.

Charles-like breakpoint system:
  - Multiple breakpoint rules, each with url_pattern + break_request + break_response + enabled
  - When a request/response matches an enabled rule, it is paused via flow.intercept()
  - The frontend can edit the body and pass/abort
  - Uses mitmproxy's native flow.intercept() / flow.resume() / flow.wait_for_resume()

Lifecycle:
  1. request phase  → emit "record" with status="pending", check breakpoints
  2. response phase → emit "record" with status="complete", check breakpoints
  3. error phase    → emit "record" with status="error"
"""
import asyncio
import base64
import contextlib
import io
import re
import sys
import json
import time
import threading
from collections import OrderedDict
from urllib.parse import urlparse
import httpx
from mitmproxy import http
from mitmproxy.options import Options
from mitmproxy.tools.dump import DumpMaster

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

_master = None
_encrypt_url = ""
_decrypt_url = ""
_capture_hosts: list[str] = []
_breakpoints: list[dict] = []  # [{url_pattern, break_request, break_response, enabled}]
_loop: asyncio.AbstractEventLoop | None = None

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
        _async_client = httpx.AsyncClient(timeout=5)
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


async def _decrypt(cipher_bytes):
    if not cipher_bytes:
        return None
    try:
        cipher_str = cipher_bytes.decode("utf-8", errors="replace").strip().strip('"')
    except Exception:
        cipher_str = str(cipher_bytes)
    # Snapshot before await so the URL used is consistent with this call.
    with _config_lock:
        decrypt_url = _decrypt_url
    if not decrypt_url:
        return cipher_str
    decrypted = await _http_post(decrypt_url, cipher_str.encode("utf-8"))
    if decrypted is None:
        # Fallback to raw decoded cipher text so the UI can still display content.
        _emit("log", {"level": "warn", "msg": "decrypt API failed, fallback to raw body text"})
        return cipher_str
    return decrypted


async def _encrypt_body(plaintext):
    with _config_lock:
        encrypt_url = _encrypt_url
    if not encrypt_url:
        _emit("log", {"level": "warn", "msg": "encrypt_url not configured, cannot re-encrypt modified body"})
        return None
    result = await _http_post(encrypt_url, plaintext.encode("utf-8"))
    if result is None:
        _emit("log", {"level": "error", "msg": "encrypt API call failed, returning None"})
        return None
    # The encrypt API returns JSON like {"data": "...cipher..."}, extract the cipher text
    try:
        obj = json.loads(result)
        if isinstance(obj, dict) and "data" in obj:
            cipher = obj["data"]
            # Wrap in quotes to match the original wire format: "ciphertext"
            return f'"{cipher}"'.encode("utf-8")
    except (json.JSONDecodeError, TypeError):
        pass
    # Fallback: use raw response as-is
    return result.encode("utf-8")


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


class SniffAddon:
    def __init__(self):
        self._flow_seq: OrderedDict[str, int] = OrderedDict()
        self._flow_start: dict[str, float] = {}
        # Cache decrypted request body so the response phase doesn't call
        # the decrypt API a second time for the same flow.
        self._flow_req_plain: dict[str, str | None] = {}

    def running(self):
        """Called by mitmproxy after it has successfully bound the port and is ready.
        This is the authoritative signal that the proxy is actually running.
        """
        _emit("status", {"running": True})
        _emit("log", {"level": "info", "msg": "mitmproxy is running and ready"})

    def log(self, entry) -> None:
        """Forward mitmproxy internal log entries to the Rust host.
        With with_termlog=False these would otherwise be silently discarded,
        leaving only the generic 'Error logged during startup' message on stderr.
        """
        level = str(entry.level)
        if level in ("error", "warn", "alert"):
            out_level = "error" if level == "alert" else level
            _emit("log", {"level": out_level, "msg": f"[mitmproxy] {entry.msg}"})

    def _get_seq(self, flow_id: str) -> int:
        if flow_id in self._flow_seq:
            return self._flow_seq[flow_id]
        seq = _next_seq()
        self._flow_seq[flow_id] = seq
        while len(self._flow_seq) > _MAX_SEEN_FLOWS:
            old_id, _ = self._flow_seq.popitem(last=False)
            self._flow_start.pop(old_id, None)
            self._flow_req_plain.pop(old_id, None)
        return seq

    async def request(self, flow: http.HTTPFlow):
        try:
            url = flow.request.pretty_url
            host, path = _parse_url(url)
            if not _host_matches(host):
                return

            seq = self._get_seq(flow.id)
            start_ms = time.time() * 1000
            self._flow_start[flow.id] = start_ms

            req_body = flow.request.content or b""
            req_plain = await _decrypt(req_body) if req_body else None
            # Cache so the response handler can reuse it without a second decrypt call.
            self._flow_req_plain[flow.id] = req_plain

            _emit("record", {
                "flow_id": flow.id, "seq": seq, "method": flow.request.method,
                "url": url, "host": host, "path": path,
                "request_content_type": _get_content_type(flow.request.headers),
                "request_headers": _safe_headers(flow.request.headers),
                "request_plain": req_plain, "request_size": len(req_body),
                "response_status": None, "response_content_type": None,
                "response_headers": None, "response_plain": None, "response_size": None,
                "start_time": start_ms, "duration": None, "status": "pending",
            })

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
                            encrypted = await _encrypt_body(new_body)
                            if encrypted is not None:
                                flow.request.content = encrypted
                            else:
                                _emit("log", {"level": "error",
                                       "msg": "encrypt failed for request, keeping original"})
        except Exception as e:
            _emit("log", {"level": "error", "msg": f"request handler error: {e}"})

    async def response(self, flow: http.HTTPFlow):
        try:
            url = flow.request.pretty_url
            host, path = _parse_url(url)
            if not _host_matches(host):
                return

            seq = self._get_seq(flow.id)
            start_ms = self._flow_start.get(flow.id, time.time() * 1000)
            duration = time.time() * 1000 - start_ms

            # Breakpoint check for response phase
            if _should_break(url, "response"):
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
                            encrypted = await _encrypt_body(new_body)
                            if encrypted is not None:
                                flow.response.content = encrypted
                            else:
                                _emit("log", {"level": "error",
                                       "msg": "encrypt failed for response, keeping original"})
                            # Body was modified — decrypt the new content for the record event.
                            resp_body = flow.response.content or b""
                            resp_plain = await _decrypt(resp_body) if resp_body else None
                # Cache the already-decrypted response body to avoid a second decrypt call below.
                _cached_resp_plain = resp_plain
                _cached_resp_body_id = id(flow.response.content)
            else:
                _cached_resp_plain = None
                _cached_resp_body_id = None

            # Recalculate duration after potential intercept wait
            duration = time.time() * 1000 - start_ms

            req_body = flow.request.content or b""
            # Reuse the cached decrypted request body from the request phase
            # to avoid a redundant decrypt API call.
            req_plain = self._flow_req_plain.pop(flow.id, None)
            resp_body = flow.response.content or b""
            # Reuse the cached decrypted response body if available and unchanged.
            if _cached_resp_plain is not None and _cached_resp_body_id == id(flow.response.content):
                resp_plain = _cached_resp_plain
            else:
                resp_plain = await _decrypt(resp_body) if resp_body else None

            _emit("record", {
                "flow_id": flow.id, "seq": seq, "method": flow.request.method,
                "url": url, "host": host, "path": path,
                "request_content_type": _get_content_type(flow.request.headers),
                "request_headers": _safe_headers(flow.request.headers),
                "request_plain": req_plain, "request_size": len(req_body),
                "response_status": flow.response.status_code,
                "response_content_type": _get_content_type(flow.response.headers),
                "response_headers": _safe_headers(flow.response.headers),
                "response_plain": resp_plain, "response_size": len(resp_body),
                "start_time": start_ms, "duration": round(duration, 1),
                "status": "complete",
            })
            self._flow_start.pop(flow.id, None)
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
            req_body = flow.request.content or b""
            # Use cached decrypted request body if available.
            req_plain = self._flow_req_plain.pop(flow.id, None)
            if req_plain is None and req_body:
                req_plain = await _decrypt(req_body)
            error_msg = str(flow.error) if flow.error else "Unknown error"
            _emit("record", {
                "flow_id": flow.id, "seq": seq, "method": flow.request.method,
                "url": url, "host": host, "path": path,
                "request_content_type": _get_content_type(flow.request.headers),
                "request_headers": _safe_headers(flow.request.headers),
                "request_plain": req_plain, "request_size": len(req_body),
                "response_status": None, "response_content_type": None,
                "response_headers": None, "response_plain": error_msg,
                "response_size": None,
                "start_time": start_ms, "duration": round(duration, 1),
                "status": "error",
            })
        except Exception as e:
            _emit("log", {"level": "error", "msg": f"error handler error: {e}"})


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


def _stdin_reader():
    """Read commands from stdin (runs in a separate thread).

    Two protocols are supported:
      1. TSV  — intercept_respond<TAB>flow_id<TAB>action<TAB>base64_body
      2. JSON — {"command": "update_config", ...}

    The TSV prefix is checked first so that a TSV line is never accidentally
    fed into the JSON parser (which would produce a misleading error).
    """
    global _encrypt_url, _decrypt_url, _capture_hosts, _breakpoints
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
                    with _config_lock:
                        url_changed = False
                        if "encrypt_url" in cmd:
                            url_changed = url_changed or (_encrypt_url != cmd["encrypt_url"])
                            _encrypt_url = cmd["encrypt_url"]
                        if "decrypt_url" in cmd:
                            url_changed = url_changed or (_decrypt_url != cmd["decrypt_url"])
                            _decrypt_url = cmd["decrypt_url"]
                        if "capture_hosts" in cmd:
                            _capture_hosts = [h for h in cmd["capture_hosts"] if h.strip()]
                        if "breakpoints" in cmd:
                            _breakpoints = cmd["breakpoints"]
                            _regex_cache.clear()
                        bp_count = len(_breakpoints)
                        hosts_snapshot = _capture_hosts
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
                        _loop.call_soon_threadsafe(
                            lambda: _loop.create_task(_rebuild_client())
                        )
                    _emit("log", {"level": "info",
                           "msg": f"config updated: {bp_count} breakpoint(s), hosts={hosts_snapshot}"})

            except json.JSONDecodeError as e:
                _emit("log", {"level": "warn", "msg": f"stdin json decode failed: {e}"})
                continue
            except Exception as e:
                _emit("log", {"level": "error", "msg": f"stdin command handling failed: {e}"})
                continue
    except Exception as e:
        _emit("log", {"level": "error", "msg": f"stdin reader crashed: {e}"})


def main():
    global _master, _encrypt_url, _decrypt_url, _capture_hosts, _breakpoints, _loop

    # Parse config from --config JSON arg
    config = {}
    args = sys.argv[1:]
    if "--config" in args:
        idx = args.index("--config")
        if idx + 1 < len(args):
            try:
                config = json.loads(args[idx + 1])
            except json.JSONDecodeError:
                _emit("log", {"level": "error", "msg": "Failed to parse --config JSON"})
                sys.exit(1)

    port = config.get("proxy_port", 8080)
    _encrypt_url = config.get("encrypt_url", "")
    _decrypt_url = config.get("decrypt_url", "")
    _capture_hosts = [h for h in config.get("capture_hosts", []) if h.strip()]
    _breakpoints = config.get("breakpoints", [])

    _emit("log", {"level": "info",
           "msg": f"starting mitmproxy on port {port}, "
                  f"{len(_breakpoints)} breakpoint(s), hosts={_capture_hosts}"})

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


if __name__ == "__main__":
    main()
