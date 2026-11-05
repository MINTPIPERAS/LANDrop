# -*- mode: python ; coding: utf-8 -*-
"""Build the Python transfer service as a ``onedir`` sidecar for the Tauri app.

The Tauri shell looks for ``landrop-service/landrop-service.exe`` next to its own
executable, so the output directory name here must stay ``landrop-service``.
Build with::

    python -m PyInstaller --noconfirm landrop-service.spec
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).resolve()  # noqa: F821 - provided by PyInstaller

# Keep the sidecar out of ../dist, which `vite build` wipes.
DIST_PATH = str(ROOT / "src-tauri" / "sidecar")
WORK_PATH = str(ROOT / "build")

# uvicorn imports its protocol implementations by string at runtime.
hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    # Paramiko resolves its crypto backend and agent transport lazily.
    "paramiko",
    "cryptography",
    "nacl",
]
hiddenimports += collect_submodules("landrop")

a = Analysis(  # noqa: F821
    [str(ROOT / "src" / "landrop" / "api" / "app.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # PySide6/PyQt are not used; excluding them keeps the bundle small.
    # ``jedi``/``parso``/``IPython`` are pulled in by site-packages scanning and
    # are never imported by the service.
    excludes=[
        "tkinter",
        "pydoc",
        "doctest",
        "PySide6",
        "PyQt5",
        "PyQt6",
        "matplotlib",
        "numpy",
        "pandas",
        "PIL",
        "pytest",
        "setuptools",
        "pip",
        "jedi",
        "parso",
        "IPython",
        "ipython",
        "zmq",
        "yaml",
        "sqlite3",
        "test",
        "lib2to3",
        "pydoc_data",
        "curses",
        "distutils",
        "xmlrpc",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="landrop-service",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # GUI subsystem: a console build makes Windows allocate a visible console
    # window every time the shell spawns the sidecar, because the parent GUI
    # process has no console to inherit. Diagnostics go to a log file instead.
    console=False,
    # Keep stderr text from turning into a modal dialog in windowed builds.
    disable_windowed_traceback=True,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    # Must match what src-tauri/src/service.rs looks for.
    name="landrop-service",
)
