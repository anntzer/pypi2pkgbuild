from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest import TestCase


_local_path = Path(__file__).parent


class TestPyPI2PKGBUILD(TestCase):

    def test_build_git(self):
        with TemporaryDirectory() as tmp_dir:
            subprocess.run(
                [sys.executable, str(_local_path / "pypi2pkgbuild.py"), "-v",
                 "-b", tmp_dir, "-I", "git+file://{}".format(_local_path)],
                check=True)

    def test_build_wheel(self):
        with TemporaryDirectory() as tmp_dir:
            subprocess.run(
                [sys.executable, "-mpip", "wheel", "--no-deps", "-w", tmp_dir,
                 str(_local_path)],
                check=True)
            wheel_path, = Path(tmp_dir).iterdir()
            subprocess.run(
                [sys.executable, str(_local_path / "pypi2pkgbuild.py"), "-v",
                 "-b", tmp_dir, "-I", "file://{}".format(wheel_path)],
                check=True)
