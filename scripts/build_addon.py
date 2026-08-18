#!/usr/bin/env python3
"""
Build script: bundles addon_bridge.py into a standalone executable via PyInstaller.

Output: src-tauri/binaries/addon_bridge-{target-triple}[.exe]

Usage:
    python scripts/build_addon.py
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

# stdout 被重定向时（CI 里就是管道），Python 在 Windows 上用的是 locale 编码 cp1252，
# 打印非 ASCII 字符会直接抛 UnicodeEncodeError。固定成 UTF-8。
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).parent.parent
ADDON_SRC = REPO_ROOT / "src-tauri" / "src" / "python" / "addon_bridge.py"
BINARIES_DIR = REPO_ROOT / "src-tauri" / "binaries"


def get_target_triple() -> str:
    """Get the current Rust target triple from rustc."""
    result = subprocess.run(
        ["rustc", "-vV"], capture_output=True, text=True, check=True
    )
    for line in result.stdout.splitlines():
        if line.startswith("host:"):
            return line.split(":", 1)[1].strip()
    raise RuntimeError("Could not determine target triple from rustc -vV")


def install_dependencies() -> None:
    """Ensure PyInstaller and the addon's runtime deps are installed.

    cryptography is a transitive dependency of mitmproxy, but the addon imports it
    directly for local encryption, so it is listed explicitly.
    """
    print("[build_addon] Installing PyInstaller + runtime deps...")
    subprocess.run(
        [
            sys.executable, "-m", "pip", "install",
            "pyinstaller", "mitmproxy", "httpx", "cryptography",
        ],
        check=True,
    )


def build_executable(triple: str) -> Path:
    """Run PyInstaller and return the path to the produced binary."""
    work_dir = REPO_ROOT / "scripts" / "_pyinstaller_work"
    dist_dir = REPO_ROOT / "scripts" / "_pyinstaller_dist"

    work_dir.mkdir(parents=True, exist_ok=True)
    dist_dir.mkdir(parents=True, exist_ok=True)

    print(f"[build_addon] Running PyInstaller for target triple: {triple}")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--onefile",
            "--name",
            "addon_bridge",
            "--distpath",
            str(dist_dir),
            "--workpath",
            str(work_dir),
            "--specpath",
            str(work_dir),
            "--noconfirm",
            str(ADDON_SRC),
        ],
        check=True,
        cwd=str(REPO_ROOT),
    )

    exe_name = "addon_bridge.exe" if sys.platform == "win32" else "addon_bridge"
    produced = dist_dir / exe_name
    if not produced.exists():
        raise FileNotFoundError(f"PyInstaller did not produce expected file: {produced}")

    return produced


def copy_to_binaries(src: Path, triple: str) -> Path:
    """Copy the built executable to src-tauri/binaries/ with the Tauri sidecar naming."""
    BINARIES_DIR.mkdir(parents=True, exist_ok=True)

    ext = ".exe" if sys.platform == "win32" else ""
    dest_name = f"addon_bridge-{triple}{ext}"
    dest = BINARIES_DIR / dest_name

    shutil.copy2(src, dest)
    print(f"[build_addon] Copied sidecar → {dest}")

    # On Windows, Defender may lock the file briefly after write.
    # Wait until it can be opened for reading before handing off to tauri-build.
    _wait_readable(dest)
    return dest


def _wait_readable(path: Path, timeout: int = 60) -> None:
    """Block until the file is readable (works around AV scan locks)."""
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with open(path, "rb") as f:
                f.read(4)
            return
        except PermissionError:
            time.sleep(1)
    raise RuntimeError(
        f"[build_addon] Timed out after {timeout}s waiting for {path} to be readable.\n"
        "Try adding the src-tauri/binaries/ directory to your antivirus exclusions."
    )


def verify(path: Path) -> None:
    """跑一遍产物的自检，确认 PyInstaller 没有漏掉依赖。

    漏掉的 import 在源码上跑不出来，只有打包后才暴露；不在这里拦住的话，通常要等
    装到用户机器上、代理起不来时才发现。
    """
    print(f"[build_addon] Running self-check on {path.name}...")
    subprocess.run([str(path), "--selftest"], check=True)


def cleanup() -> None:
    """Remove PyInstaller temp directories."""
    for d in [
        REPO_ROOT / "scripts" / "_pyinstaller_work",
        REPO_ROOT / "scripts" / "_pyinstaller_dist",
    ]:
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
            print(f"[build_addon] Cleaned up {d}")


def main() -> None:
    triple = get_target_triple()
    print(f"[build_addon] Target triple: {triple}")

    install_dependencies()
    produced = build_executable(triple)
    dest = copy_to_binaries(produced, triple)
    verify(dest)
    cleanup()

    print(f"\n[build_addon] Done! Sidecar ready at:\n  {dest}")
    print("\nNext steps:")
    print("  npm run build:win   (Windows)")
    print("  npm run build:mac   (macOS)")


if __name__ == "__main__":
    main()
