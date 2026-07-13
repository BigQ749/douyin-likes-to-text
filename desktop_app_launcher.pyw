"""Windows GUI launcher that uses the base GUI interpreter, not the console venv shim."""
from __future__ import annotations

import runpy
import site

from app_paths import ROOT, VENDOR_ROOT

APP_VENV_SITE = ROOT / ".venv" / "Lib" / "site-packages"
VENDOR_VENV_SITE = VENDOR_ROOT / ".venv" / "Lib" / "site-packages"

# uv-created virtual environments may ship a console-subsystem pythonw.exe.
# Loading their site-packages from the real CPython GUI interpreter keeps the
# desktop app dependency-compatible without opening a black console window.
for site_packages in (APP_VENV_SITE, VENDOR_VENV_SITE):
    if site_packages.exists():
        site.addsitedir(str(site_packages))

runpy.run_path(str(ROOT / "desktop_app.py"), run_name="__main__")
