"""Portable launcher and documented launchd installation contract."""
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX launcher")


def test_launcher_uses_clone_venv_from_other_directory(tmp_path):
    clone = tmp_path / "clone with spaces & symbols"
    (clone / "app").mkdir(parents=True)
    launcher = clone / "app" / "launcher.sh"
    shutil.copy2(ROOT / "app" / "launcher.sh", launcher)
    assert os.access(launcher, os.X_OK)
    missing = subprocess.run([str(launcher)], cwd=tmp_path, capture_output=True, text=True)
    assert missing.returncode == 1
    assert str(clone / ".venv/bin/python") in missing.stderr
    assert "README.md Setup" in missing.stderr

    python = clone / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text('#!/bin/bash\npwd\nprintf "%s\\n" "$@"\n')
    python.chmod(0o755)
    result = subprocess.run([str(launcher)], cwd=tmp_path, capture_output=True, text=True, check=True)
    assert result.stdout.splitlines() == [
        str(clone.resolve()), "-m", "uvicorn", "app.main:app",
        "--host", "127.0.0.1", "--port", "8000",
    ]
    assert "conda" not in launcher.read_text().lower()
    assert "/Users/" not in launcher.read_text()


def test_documented_plist_generation_records_clone_and_home(tmp_path):
    clone = tmp_path / "clone & spaces"
    (clone / "app").mkdir(parents=True)
    name = "com.joeelhajj.investmentengine.plist"
    shutil.copy2(ROOT / "app" / name, clone / "app" / name)
    home = tmp_path / "test home"
    doc = (ROOT / "app/INSTALL.md").read_text()
    code = doc.split(".venv/bin/python - <<'PY'\n", 1)[1].split("\nPY", 1)[0]
    subprocess.run([sys.executable, "-c", code], cwd=clone,
                   env={**os.environ, "HOME": str(home)}, check=True)
    target = home / "Library/LaunchAgents" / name
    with target.open("rb") as source:
        job = plistlib.load(source)
    assert job["ProgramArguments"] == ["/bin/bash", str(clone.resolve() / "app/launcher.sh")]
    assert job["StandardOutPath"] == job["StandardErrorPath"] == str(home / ".investment_engine/server.log")
    assert job["RunAtLoad"] is True and job["KeepAlive"] is True
    assert (home / ".investment_engine").is_dir()
    assert "/Users/" not in (clone / "app" / name).read_text()
    if sys.platform == "darwin":
        subprocess.run(["/usr/bin/plutil", "-lint", str(target)], check=True)
