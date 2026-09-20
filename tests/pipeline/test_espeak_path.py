"""espeak-ng cannot open its data from a path of 160+ characters, and instead
of an error it exits the process. These keep a deeply nested checkout working."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from voice_agent.pipeline import factory


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(factory, "ESPEAK_LINK_ROOT", tmp_path / "cache")
    return tmp_path / "cache"


def data_dir(root: Path, length: int) -> Path:
    """A real espeak-ng-data directory whose path is exactly `length` characters."""
    pad = length - len(str(root)) - len("/espeak-ng-data") - 1
    real = root / ("a" * pad) / "espeak-ng-data"
    real.mkdir(parents=True)
    (real / "phontab").write_bytes(b"x")
    assert len(str(real)) == length
    return real


def test_a_path_that_fits_is_used_as_is(tmp_path, cache):
    real = data_dir(tmp_path / "r", factory.ESPEAK_MAX_PATH)
    assert factory.espeak_safe_data_dir(real) == real
    assert not cache.exists()


def test_a_path_that_is_too_long_becomes_a_short_copy_of_the_same_data(tmp_path, cache):
    real = data_dir(tmp_path / "r", factory.ESPEAK_MAX_PATH + 1)
    (real / "lang").mkdir()
    (real / "lang" / "en").write_bytes(b"english")
    safe = factory.espeak_safe_data_dir(real)
    assert len(str(safe)) <= factory.ESPEAK_MAX_PATH
    # A real directory, not a link: phonemizer resolves symlinks back to the long path.
    assert not safe.is_symlink() and safe.resolve() == safe
    assert (safe / "phontab").read_bytes() == b"x"
    assert (safe / "lang" / "en").read_bytes() == b"english"


def test_the_copy_is_made_once_and_repaired_when_damaged(tmp_path, cache):
    real = data_dir(tmp_path / "r", 200)
    first = factory.espeak_safe_data_dir(real)
    marker = first / "phontab"
    marker.write_bytes(b"x")
    before = marker.stat().st_mtime_ns
    assert factory.espeak_safe_data_dir(real) == first
    assert marker.stat().st_mtime_ns == before          # reused, not recopied
    marker.write_bytes(b"truncated by an interrupted copy")
    assert factory.espeak_safe_data_dir(real) == first
    assert marker.read_bytes() == b"x"


def test_an_old_symlink_from_an_earlier_version_is_replaced(tmp_path, cache):
    real = data_dir(tmp_path / "r", 200)
    safe = factory.espeak_safe_data_dir(real)
    shutil_rm = __import__("shutil").rmtree
    shutil_rm(safe.parent)
    safe.parent.mkdir(parents=True)
    safe.symlink_to(real, target_is_directory=True)
    fixed = factory.espeak_safe_data_dir(real)
    assert not fixed.is_symlink() and (fixed / "phontab").is_file()


def test_different_checkouts_do_not_share_a_copy(tmp_path, cache):
    a = factory.espeak_safe_data_dir(data_dir(tmp_path / "a", 200))
    b = factory.espeak_safe_data_dir(data_dir(tmp_path / "b", 200))
    assert a != b


def test_an_unusably_deep_cache_gives_a_plain_instruction(tmp_path, monkeypatch):
    monkeypatch.setattr(factory, "ESPEAK_LINK_ROOT", tmp_path / ("d" * 200))
    with pytest.raises(RuntimeError, match="shorter path"):
        factory.espeak_safe_data_dir(data_dir(tmp_path / "r", 200))


def test_espeak_actually_starts_from_the_safe_path_for_this_checkout():
    """The real library, the real data, this checkout's real location."""
    import espeakng_loader
    safe = factory.espeak_safe_data_dir(Path(espeakng_loader.get_data_path()))
    code = "\n".join([
        "import ctypes, sys",
        f"lib = ctypes.cdll.LoadLibrary({espeakng_loader.get_library_path()!r})",
        f"rate = lib.espeak_Initialize(2, 0, {str(safe)!r}.encode(), 0)",
        "sys.exit(0 if rate > 0 else 1)",
    ])
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
