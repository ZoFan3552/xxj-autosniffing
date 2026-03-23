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
```

**Runtime requirements** (only needed when sidecar is NOT bundled, i.e. during development):
- `python` or `python3` on PATH with `pip install mitmproxy httpx`
- `adb` on PATH for Android device support

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
- **`commands.rs`** — Tauri command handlers (`get_config`, `set_config`, `proxy_start`, `proxy_stop`, `intercept_respond`, `adb_status`); holds shared `AppState` (all fields behind `parking_lot::Mutex`)
- **`proxy_runner.rs`** — Two launch modes: (1) bundled sidecar executable (`find_sidecar()`), or (2) fallback to system Python with `include_str!("python/addon_bridge.py")` written to a temp file. Reader threads parse JSON lines from stdout and emit Tauri events. `InterceptBridge` writes intercept responses and config updates to Python via stdin
- **`adb_manager.rs`** — Polls `adb devices` every 3 seconds; calls `adb shell settings put global http_proxy` and `adb reverse` to route Android traffic through the proxy
- **`config.rs`** — `Config` struct persisted to `~/.config/xxj-auto-sniffing/config.json`. `merge_from()` patches fields via JSON merge without full replacement. Default capture host: `xunfeixxj.com`
- **`models.rs`** — Shared Rust types for intercept payloads (`InterceptAction::Pass|Abort`)

### Python Addon (`src-tauri/src/python/addon_bridge.py`)

Runs as a mitmproxy `DumpMaster` with a custom `SniffAddon`. Can be bundled into a standalone executable via `python scripts/build_addon.py` (PyInstaller → `src-tauri/binaries/addon_bridge-{target-triple}[.exe]`).

- **Stdout protocol**: newline-delimited JSON `{"event": "record"|"intercept"|"status"|"log", "data": {...}}`
- **Stdin protocol**: two formats:
  - TSV: `intercept_respond\t<flow_id>\t<action>\t<base64_body>`
  - JSON: `{"command": "update_config", ...}` for live config hot-reload
- **Decrypt/encrypt hooks**: for each request/response body, calls the configured `decrypt_url` (POST, plain text) to get plaintext; on intercept pass with modified body, calls `encrypt_url` to re-encrypt before forwarding
- **Breakpoints**: Charles-style rules (regex `url_pattern`, `break_request`/`break_response` flags); matching flows are paused via `flow.intercept()` and an `asyncio.Event` wait with 120s timeout

### Frontend (`src/`)

- **`App.tsx`** — Root component; holds all state; listens to Tauri events (`record`, `intercept`, `proxy_status`, `proxy_error`, `adb_status`); caps traffic at 2000 records and 200 intercepts in memory
- **`types.ts`** — Canonical TypeScript types: `Config`, `RequestRecord`, `InterceptRequest`, `BreakpointRule`
- **`components/TrafficPanel.tsx`** — Virtualized traffic list + `DetailPanel` split view
- **`components/DetailPanel.tsx`** — Request/response detail viewer (overview, headers, body tabs)
- **`components/InterceptPanel.tsx`** — Pending intercepts with editable body and pass/abort controls
- **`components/SettingsPanel.tsx`** — Proxy port, capture host allowlist, encrypt/decrypt URLs, breakpoint rule editor
- **`components/JsonEditorModal.tsx`** — Monaco-based JSON editor modal for mock region editing
- **`components/ResizableSplit.tsx`** — Draggable split pane layout
- **`utils/format.ts`** — Utility formatting helpers

### Key Design Decisions

- **Sidecar-first**: `ProxyRunner` checks for a bundled PyInstaller sidecar next to the app binary; only falls back to system Python if absent. Production builds run `build_addon.py` automatically via npm scripts
- Config changes via `set_config` are **hot-reloaded** into the running Python process via the stdin JSON command channel (no restart required). Sensitive fields (`encrypt_url`, `decrypt_url`) are sent via stdin only, not via CLI args
- The `proxy_status: {running: true}` event is emitted by Python's `SniffAddon.running()` hook only after mitmproxy successfully binds the port — not optimistically by Rust
- ADB setup (`http_proxy` + `adb reverse`) is triggered automatically when `proxy_start` succeeds and a device is connected
- On Windows, proxy stop uses `taskkill /F /T` to kill the entire process tree (needed for PyInstaller --onefile sidecars)
