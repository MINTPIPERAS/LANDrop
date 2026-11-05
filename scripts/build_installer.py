#!/usr/bin/env python
"""Build the Windows installer: Python sidecar first, then the Tauri bundle.

Run from the repository root::

    python scripts/build_installer.py

The order matters: the Tauri resource mapping embeds
``src-tauri/sidecar/landrop-service``, so the sidecar must exist before the
bundle step runs.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SIDECAR = ROOT / "src-tauri" / "sidecar" / "landrop-service" / "landrop-service.exe"
INSTALLER_DIR = ROOT / "src-tauri" / "target" / "release" / "bundle"


def run(command: list[str], description: str) -> None:
    print(f"\n=== {description} ===", flush=True)
    print(" ".join(command), flush=True)
    result = subprocess.run(command, cwd=ROOT)
    if result.returncode != 0:
        raise SystemExit(f"{description} 失败，退出码 {result.returncode}")


def main() -> int:
    npm = shutil.which("npm")
    if npm is None:
        raise SystemExit("找不到 npm，请先安装 Node.js 并加入 PATH")

    # 1. Build the Python sidecar (onedir, matching the Tauri resource mapping).
    run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm",
         "--distpath", "src-tauri/sidecar", "--workpath", "build",
         "landrop-service.spec"],
        "打包 Python 传输服务 (PyInstaller onedir)",
    )
    if not SIDECAR.is_file():
        raise SystemExit(f"sidecar 未生成: {SIDECAR}")

    # 2. Build the desktop app and installer (also builds the frontend).
    print("\n首次打包时 Tauri 会下载 NSIS 工具链；若网络无法访问 GitHub，"
          "请先把 NSIS 缓存放到 %LOCALAPPDATA%\\tauri\\NSIS，详见 docs/技术实现.md 的打包章节。",
          flush=True)
    run([npm, "run", "tauri", "--", "build"], "构建 Tauri 应用与 Windows 安装包")

    # 3. Report what was produced.
    installers = sorted(INSTALLER_DIR.rglob("*.exe")) if INSTALLER_DIR.is_dir() else []
    print("\n=== 产物 ===", flush=True)
    if not installers:
        print(f"未找到安装包，请检查 {INSTALLER_DIR}", flush=True)
        return 1
    for path in installers:
        size = path.stat().st_size / (1024 * 1024)
        print(f"  {path}  ({size:.1f} MB)", flush=True)
    return 0


if __name__ == "__main__":
    os.chdir(ROOT)
    sys.exit(main())
