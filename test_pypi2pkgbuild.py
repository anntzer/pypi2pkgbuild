from pathlib import Path
import subprocess
import sys


_local_path = Path(__file__).parent


def test_build_git(tmp_path):
    subprocess.run(
        [sys.executable, str(_local_path / "pypi2pkgbuild.py"),
         "-b", tmp_path, "-I", "git+file://{}".format(_local_path)],
        check=True)


def test_build_wheel(tmp_path):
    subprocess.run(
        [sys.executable, "-mpip", "wheel", "--no-deps", "-w", tmp_path,
         str(_local_path)],
        check=True)
    wheel_path, = tmp_path.iterdir()
    subprocess.run(
        [sys.executable, str(_local_path / "pypi2pkgbuild.py"),
         "-b", tmp_path, "-I", "file://{}".format(wheel_path)],
        check=True)
