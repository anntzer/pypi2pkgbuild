import functools
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest import TestCase


_local_path = Path(__file__).parent
_run = functools.partial(subprocess.run, check=True)


class TestPyPI2PKGBUILD(TestCase):

    def test_build_git(self):
        for makepkg_opts in ["", "--nobuild"]:
            with self.subTest(makepkg_opts=makepkg_opts), \
                 TemporaryDirectory() as tmp_dir:
                _run([sys.executable, _local_path / "pypi2pkgbuild.py",
                      "-v", "-I", f"-m={makepkg_opts}", "-b", tmp_dir,
                      f"git+file://{_local_path}"])

    def test_build_sdist_wheel(self):
        env = {"PIP_CONFIG_FILE": "/dev/null", **os.environ}
        for makepkg_opts in ["", "--nobuild"]:
            with self.subTest(makepkg_opts=makepkg_opts), \
                 TemporaryDirectory() as tmp_dir:
                tmp_path = Path(tmp_dir)
                _run([sys.executable, "-mvenv", tmp_path])
                _run([tmp_path / "bin/pip", "install", "build"], env=env)
                _run([tmp_path / "bin/pyproject-build", _local_path,
                      "-o", tmp_path / "dist"], env=env)
                sdist_path, = tmp_path.glob("dist/*.tar.gz")
                wheel_path, = tmp_path.glob("dist/*.whl")
                _run([sys.executable, _local_path / "pypi2pkgbuild.py",
                      "-v", "-I", f"-m={makepkg_opts}", "-b", tmp_path / "s",
                      f"file://{sdist_path}"])
                _run([sys.executable, _local_path / "pypi2pkgbuild.py",
                      "-v", "-I", f"-m={makepkg_opts}", "-b", tmp_path / "w",
                      f"file://{wheel_path}"])
