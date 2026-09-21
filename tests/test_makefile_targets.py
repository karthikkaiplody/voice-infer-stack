"""Every target that serves the page builds it first, so a fresh clone never gets a 503."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from voice_agent import paths

pytestmark = pytest.mark.skipif(shutil.which("make") is None, reason="needs make")


def _plan(target):
    """What `make <target>` would run, without running it."""
    done = subprocess.run(["make", "-n", "-C", str(paths.REPO_ROOT), target],
                          capture_output=True, text=True, check=True)
    return done.stdout


@pytest.mark.parametrize("target", ["demo", "live", "demo-live"])
def test_serving_targets_build_the_page_before_the_server_starts(target):
    plan = _plan(target)
    assert "npm --prefix ui run build" in plan
    assert plan.index("npm --prefix ui run build") < plan.index("voice_agent.server.live")
