# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Frontend dev server (Vite, no Tauri)
npm run dev

# Run full Tauri app in development mode
npm run tauri dev

# Build for Windows (no installer bundle)
npm run build:win          # → tauri build --no-bundle

# Build for macOS (app + dmg)
npm run build:mac          # → tauri build --bundles app,dmg

# Build frontend only
npm run build              # → tsc && vite build
```

**Python dependencies** (required at runtime, not bundled):
```bash
pip install mitmproxy httpx
```

**Runtime requirements**: `python` or `python3` on PATH, `adb` on PATH for Android device support.

## Architecture Overview

This is a **Tauri v2** desktop app (Rust + React/TypeScript) that provides HTTP traffic sniffing, decryption, and interception for mobile apps.

### Data Flow

```
Android device (ADB)
    ↕ TCP reverse proxy + HTTP proxy settings
mitmproxy (Python subprocess)
    ↕ stdout (JSON events) / stdin (TSV + JSON commands)
Rust backend (Tauri commands + event emitter)
    ↕ Tauri IPC (invoke/listen)
React frontend
```

### Rust Backend (`src-tauri/src/`)

- **`lib.rs`** — App entry point: initializes `AppState`, starts ADB polling, registers Tauri commands
- **`commands.rs`** — Tauri command handlers (`get_config`, `set_config`, `proxy_start`, `proxy_stop`, `intercept_respond`, `adb_status`); holds shared `AppState`
- **`proxy_runner.rs`** — Spawns the Python mitmproxy subprocess; reader threads parse JSON lines from stdout and emit Tauri events (`record`, `intercept`, `proxy_status`, `proxy_error`). `InterceptBridge` writes intercept responses back to Python via stdin
- **`adb_manager.rs`** — Polls `adb devices` every 3 seconds; calls `adb shell settings put global http_proxy` and `adb reverse` to route Android traffic through the proxy
- **`config.rs`** — `Config` struct persisted to `~/.config/xxj-auto-sniffing/config.json`. `merge_from()` patches fields without full replacement
- **`models.rs`** — Shared Rust types for intercept payloads

### Python Addon (`src-tauri/src/python/addon_bridge.py`)

Embedded via `include_str!()` and written to a temp file at runtime. Runs as a mitmproxy `DumpMaster` with a custom `SniffAddon`:

- **Stdout protocol**: emits newline-delimited JSON `{"event": "record"|"intercept"|"status"|"log", "data": {...}}`
- **Stdin protocol**: two formats:
  - TSV: `intercept_respond\t<flow_id>\t<action>\t<base64_body>`
  - JSON: `{"command": "update_config", ...}` for live config hot-reload
- **Decrypt/encrypt hooks**: for each request/response body, calls the configured `decrypt_url` (POST, plain text) to get plaintext; on intercept pass with modified body, calls `encrypt_url` to re-encrypt before forwarding
- **Breakpoints**: Charles-style rules (regex `url_pattern`, `break_request`/`break_response` flags); matching flows are paused via `flow.intercept()` and an `asyncio.Event` wait with 120s timeout

### Frontend (`src/`)

- **`App.tsx`** — Single root component; holds all state; listens to Tauri events (`record`, `intercept`, `proxy_status`, `proxy_error`, `adb_status`); caps traffic at 2000 records and 200 intercepts in memory
- **`types.ts`** — Canonical TypeScript types: `Config`, `RequestRecord`, `InterceptRequest`, `BreakpointRule`
- **`components/TrafficPanel.tsx`** — Virtualized traffic list + `DetailPanel` split view
- **`components/InterceptPanel.tsx`** — Pending intercepts with editable body and pass/abort controls
- **`components/SettingsPanel.tsx`** — Proxy port, capture host allowlist, encrypt/decrypt URLs, breakpoint rule editor
- **`utils/format.ts`** — Utility formatting helpers

### Key Design Decisions

- The Python script is **embedded** in the Rust binary (`include_str!`), extracted to a temp file on each proxy start — no separate distribution of the Python file needed
- Config changes via `set_config` are **hot-reloaded** into the running Python process via the stdin JSON command channel (no restart required)
- The `proxy_status: {running: true}` event is emitted by Python's `SniffAddon.running()` hook only after mitmproxy successfully binds the port — not optimistically by Rust
- ADB setup (`http_proxy` + `adb reverse`) is triggered automatically when `proxy_start` succeeds and a device is connected
