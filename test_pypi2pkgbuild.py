from pathlib import Path
import subprocess
import sys


_local_path = Path(__file__).parent


def test_build_git(tmpdir):
    subprocess.run(
        [sys.executable, str(_local_path / "pypi2pkgbuild.py"),
         "-b", str(tmpdir), "-n", "git+file://{}".format(_local_path)],
        check=True)


def test_build_wheel(tmpdir):
    subprocess.run(
        [sys.executable, "-mpip", "wheel", "--no-deps", "-w", str(tmpdir),
         str(_local_path)],
        check=True)
    wheel_path, = Path(tmpdir).iterdir()
    subprocess.run(
        [sys.executable, str(_local_path / "pypi2pkgbuild.py"),
         "-b", str(tmpdir), "-n", "file://{}".format(wheel_path)],
        check=True)
