# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Frontend dev server (Vite, no Tauri)
npm run dev

# Run full Tauri app in development mode
npm run tauri dev

# Build Python addon into a bundled sidecar (PyInstaller)
python scripts/build_addon.py

# Build for Windows (NSIS + MSI installers) — runs build_addon.py automatically
npm run build:win

# Build for Windows (no installer bundle)
npm run build:win:noinstaller

# Build for macOS (app + dmg) — runs build_addon.py automatically
npm run build:mac

# Build frontend only
npm run build              # → tsc && vite build

# Self-checks for the Python addon
python src-tauri/src/python/addon_bridge.py --selftest   # local crypto, merge patch, rule matching
python scripts/e2e_addon.py                              # real mitmproxy + local upstream, 8 cases

# Same 8 cases against the PyInstaller sidecar instead of the source, to catch missing imports
XXJ_ADDON_CMD=src-tauri/binaries/addon_bridge-$(rustc -vV | sed -n 's/^host: //p') python scripts/e2e_addon.py
```

**Runtime requirements** (only needed when sidecar is NOT bundled, i.e. during development):
- `python` or `python3` on PATH with `pip install mitmproxy httpx cryptography`
- `adb` on PATH for Android device support
- `pip install websockets` additionally to run the WebSocket cases of `scripts/e2e_addon.py`

## Architecture Overview

This is a **Tauri v2** desktop app (Rust + React/TypeScript) that provides HTTP traffic sniffing, decryption, and interception for mobile apps.

### Data Flow

```
Android device (ADB)
    ↕ TCP reverse proxy + HTTP proxy settings
mitmproxy (Python subprocess OR bundled sidecar)
    ↕ stdout (JSON events) / stdin (TSV + JSON commands)
Rust backend (Tauri commands + event emitter)
    ↕ Tauri IPC (invoke/listen)
React frontend
```

### Rust Backend (`src-tauri/src/`)

- **`lib.rs`** — App entry point: initializes `AppState`, starts ADB polling, registers Tauri commands. Handles `RunEvent::Exit` to stop proxy and teardown ADB
- **`commands.rs`** — Tauri command handlers (`get_config`, `set_config`, `proxy_start`, `proxy_stop`, `intercept_respond`, `outbound_send`, `ws_replay_arm`, `adb_status`); holds shared `AppState` (all fields behind `parking_lot::Mutex`)
- **`proxy_runner.rs`** — Two launch modes: (1) bundled sidecar executable (`find_sidecar()`), or (2) fallback to system Python with `include_str!("python/addon_bridge.py")` written to a temp file. Reader threads parse JSON lines from stdout and emit Tauri events. `InterceptBridge` writes intercept responses and config updates to Python via stdin
- **`adb_manager.rs`** — Polls `adb devices` every 3 seconds; calls `adb shell settings put global http_proxy` and `adb reverse` to route Android traffic through the proxy
- **`config.rs`** — `Config` struct persisted to `~/.config/xxj-auto-sniffing/config.json`. `merge_from()` patches fields via JSON merge without full replacement. `update_command()` builds the `update_config` line pushed to Python over stdin. Default capture host: `xunfeixxj.com`
- **`models.rs`** — Shared Rust types for intercept payloads (`InterceptAction::Pass|Abort`)

### Python Addon (`src-tauri/src/python/addon_bridge.py`)

Runs as a mitmproxy `DumpMaster` with a custom `SniffAddon`. Can be bundled into a standalone executable via `python scripts/build_addon.py` (PyInstaller → `src-tauri/binaries/addon_bridge-{target-triple}[.exe]`).

- **Stdout protocol**: newline-delimited JSON `{"event": "record"|"intercept"|"status"|"log"|"ws_conn"|"ws_frame"|"outbound_result", "data": {...}}`
- **Stdin protocol**: two formats:
  - TSV: `intercept_respond\t<flow_id>\t<action>\t<base64_body>`
  - JSON: `{"command": "update_config"|"ws_replay"|"outbound", ...}`
- **Self-checks**: `--selftest` covers the pure logic; `scripts/e2e_addon.py` runs 8 cases against a real mitmproxy and a local upstream
- **Crypto backends**, in priority order: local secret (AES-128-ECB over gzip, `SHA1(secret)[:16]` as the key, in-process) → `encrypt_url`/`decrypt_url` HTTP endpoints → plaintext passthrough. `_encrypt_body()` mirrors the quoting of the cipher body it replaces (both bare base64 and a quoted JSON string occur on the wire)
- **Mock rules**: checked in the request phase *before* breakpoints, so a matched rule suppresses them. `respond` sets `flow.response` without contacting the server; `patch` merges a JSON fragment into the real response per RFC 7386. Hit counts are kept per rule fingerprint so editing one rule does not reset the others. A crypto or JSON failure on this path fails closed with 502
- **Breakpoints**: Charles-style rules (regex `url_pattern`, `break_request`/`break_response` flags); matching flows are paused via `flow.intercept()` and an `asyncio.Event` wait with 120s timeout
- **WebSocket**: every frame of a captured connection is emitted as `ws_frame`; a replay armed for a URL path drops that connection's real frames in *both* directions and injects the recorded downstream frames at their original relative times. A replay is consumed by the first connection that handshakes on its path
- **Outbound**: the `outbound` command encrypts the supplied plaintext body, sends the request back through this proxy's own port (so it is recorded and matched against mock rules), decrypts the response and emits `outbound_result`. The `x-xxj-origin` header marks it and is stripped before forwarding

### Frontend (`src/`)

- **`App.tsx`** — Root component; holds all state; listens to Tauri events (`record`, `intercept`, `proxy_status`, `proxy_error`, `adb_status`, `ws_conn`, `ws_frame`, `outbound_result`); caps traffic at 2000 records, 200 intercepts and 5000 WS frames in memory
- **`types.ts`** — Canonical TypeScript types: `Config`, `RequestRecord`, `InterceptRequest`, `BreakpointRule`, `MockRule`, `WsConn`, `WsFrame`, `Identity`, `OutboundResult`
- **`components/TrafficPanel.tsx`** — Virtualized traffic list + `DetailPanel` split view
- **`components/DetailPanel.tsx`** — Request/response detail viewer (overview, headers, body tabs)
- **`components/InterceptPanel.tsx`** — Pending intercepts with editable body and pass/abort controls
- **`components/SettingsPanel.tsx`** — Proxy port, capture host allowlist, local crypto secret, encrypt/decrypt URLs, breakpoint and mock rule editors
- **`components/WsPanel.tsx`** — WebSocket connections, their frames, and the replay arming button
- **`components/OutboundPanel.tsx`** — Compose and send an outbound request from a derived identity
- **`components/JsonEditorModal.tsx`** — Monaco-based JSON editor modal for mock region editing
- **`components/ResizableSplit.tsx`** — Draggable split pane layout
- **`utils/format.ts`** — Utility formatting helpers
- **`utils/identity.ts`** — Derives per-host identity snapshots (credential headers + `base` envelope) from the records already in memory, and builds outbound bodies with fresh trace ids. No backend state involved

### Key Design Decisions

- **Sidecar-first**: `ProxyRunner` checks for a bundled PyInstaller sidecar next to the app binary; only falls back to system Python if absent. Production builds run `build_addon.py` automatically via npm scripts
- Config changes via `set_config` are **hot-reloaded** into the running Python process via the stdin JSON command channel (no restart required). Sensitive fields (`encrypt_url`, `decrypt_url`, `crypto_secret`) are sent via stdin only, not via CLI args
- **Identity snapshots live in the frontend**, derived from the records it already holds, rather than being tracked in Python. The device is not rooted and the app cannot be `run-as`, so proxied traffic is the only source either way — deriving them where the data already is avoids a second copy and a request/response round trip
- Mock rules win over breakpoints: a rule already states what to answer, so there is nothing left for a human to decide
- Outbound requests skip breakpoints — pausing a request this app fired itself would only stall the caller waiting on it
- The `proxy_status: {running: true}` event is emitted by Python's `SniffAddon.running()` hook only after mitmproxy successfully binds the port — not optimistically by Rust
- ADB setup (`http_proxy` + `adb reverse`) is triggered automatically when `proxy_start` succeeds and a device is connected
- On Windows, proxy stop uses `taskkill /F /T` to kill the entire process tree (needed for PyInstaller --onefile sidecars)
