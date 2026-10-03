import os
import stat
import sys
from pathlib import Path

import pytest

FAKE = Path(__file__).with_name("fake_bdpan.py")


@pytest.fixture(autouse=True)
def disable_smart_download():
    """测试时默认禁用智能下载功能"""
    old_value = os.environ.get("BAIDU_EASY_SMART_DOWNLOAD")
    os.environ["BAIDU_EASY_SMART_DOWNLOAD"] = "0"
    yield
    if old_value is None:
        os.environ.pop("BAIDU_EASY_SMART_DOWNLOAD", None)
    else:
        os.environ["BAIDU_EASY_SMART_DOWNLOAD"] = old_value


@pytest.fixture
def fake_bin(tmp_path: Path) -> str:
    wrapper = tmp_path / "bdpan"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
    return str(wrapper)
