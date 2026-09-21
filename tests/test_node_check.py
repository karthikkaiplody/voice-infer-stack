"""The Makefile's Node check: right verdict at every boundary, and in step with the UI.

`make setup-demo`, `make demo` and the rest stop up front when Node cannot build the
page. These tests run that check against a stand-in `node`, so no old Node needs
installing, and pin the limits to `engines` in ui/package.json.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from voice_agent import paths

ROOT = paths.REPO_ROOT
MAKEFILE = ROOT / "Makefile"

pytestmark = pytest.mark.skipif(shutil.which("make") is None, reason="needs make")


def _check_node(tmp_path, version):
    """Run `make check-node` with a `node` that reports `version`, or none at all."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    if version is not None:
        node = bin_dir / "node"
        node.write_text(f'#!/bin/sh\n[ "$1" = "-p" ] && echo {version}\n')
        node.chmod(0o755)
    # Only the system directories after it, so no real Node is found.
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path)}
    return subprocess.run(["make", "-s", "-C", str(ROOT), "check-node"],
                          env=env, capture_output=True, text=True)


@pytest.mark.parametrize("version", [
    "20.19.0", "20.20.1", "22.12.0", "22.22.3", "23.0.0", "24.1.0", "26.0.0"])
def test_supported_versions_pass(tmp_path, version):
    assert _check_node(tmp_path, version).returncode == 0


@pytest.mark.parametrize("version", [
    "16.20.2", "18.19.1", "20.18.9", "21.7.3", "22.11.0"])
def test_unsupported_versions_stop_with_the_fix(tmp_path, version):
    result = _check_node(tmp_path, version)
    assert result.returncode != 0
    assert version in result.stdout
    assert "20.19+ or 22.12+" in result.stdout
    assert "brew install node" in result.stdout and "nvm install" in result.stdout


def test_missing_node_says_to_install_it(tmp_path):
    result = _check_node(tmp_path, None)
    assert result.returncode != 0
    assert "not installed" in result.stdout and "brew install node" in result.stdout


def test_limits_match_the_uis_engines_field():
    engines = json.loads((ROOT / "ui" / "package.json").read_text())["engines"]["node"]
    found = re.fullmatch(r"\^20\.(\d+)\.0 \|\| >=22\.(\d+)\.0", engines)
    assert found, f"engines changed shape ({engines!r}); update the Makefile check to match"
    makefile = MAKEFILE.read_text()
    assert f"NODE_20_MIN_MINOR = {found.group(1)}" in makefile
    assert f"NODE_22_MIN_MINOR = {found.group(2)}" in makefile
