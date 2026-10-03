import stat
import sys
from pathlib import Path

import pytest

FAKE = Path(__file__).with_name("fake_bdpan.py")


@pytest.fixture
def fake_bin(tmp_path: Path) -> str:
    wrapper = tmp_path / "bdpan"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
    return str(wrapper)
