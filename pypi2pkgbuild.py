#!/usr/bin/env python
from argparse import ArgumentParser, ArgumentDefaultsHelpFormatter
from collections import namedtuple, OrderedDict
from functools import lru_cache, partial
import hashlib
from io import StringIO
import json
import logging
from pathlib import Path
import re
import shlex
import shutil
import subprocess
from subprocess import PIPE
import sys
from tempfile import TemporaryDirectory
import urllib.request


LOGGER = logging.getLogger(Path(__file__).stem)

PY_TAGS = ["py2.py3",
           "py{0.major}".format(sys.version_info),
           "cp{0.major}{0.minor}".format(sys.version_info)]
PLATFORM_TAGS = {
    "any": "any", "manylinux1_i686": "i686", "manylinux1_x86_64": "x86_64"}
SDIST_SUFFIXES = [".tar.gz", ".tar.bz2", ".zip"]
WHEEL_SUFFIX = ".whl"
LICENSE_NAMES = ["LICENSE", "LICENSE.txt", "COPYING.rst"]
TROVE_COMMON_LICENSES = {  # Licenses provided by base `licenses` package.
    "GNU Affero General Public License v3":
        "AGPL3",
    "GNU Affero General Public License v3 or later (AGPLv3+)":
        "AGPL3",
    "Apache Software License":
        "Apache",
    "Artistic License":
        "Artistic2.0",
    # "CCPL",
    # "CDDL",
    # "EPL",
    # "FDL1.2",  # See FDL1.3.
    "GNU Free Documentation License (FDL)":
        "FDL1.3",
    "GNU General Public License v2 (GPLv2)":
        "GPL2",
    "GNU General Public License v2 or later (GPLv2+)":
        "GPL2",
    "GNU General Public License v3 (GPLv3)":
        "GPL3",
    "GNU General Public License v3 or later (GPLv3+)":
        "GPL3",
    "GNU Lesser General Public License v2 (GPLv2)":
        "LGPL2.1",
    "GNU Lesser General Public License v2 or later (GPLv2+)":
        "LGPL2.1",
    "GNU Lesser General Public License v3 (GPLv3)":
        "LGPL3",
    "GNU Lesser General Public License v3 or later (GPLv3+)":
        "LGPL3",
    # "LPPL",
    "Mozilla Public License 1.1 (MPL 1.1)":
        "MPL",
    # "PerlArtistic",  # See Artistic2.0.
    # "PHP",
    "Python Software Foundation License":
        "PSF",
    # "RUBY",
    "W3C License":
        "W3C",
    "Zope Public License":
        "ZPL",
}
TROVE_SPECIAL_LICENSES = {  # Standard licenses with specific line.
    "BSD License":
        "BSD",
    "MIT License":
        "MIT",
    "zlib/libpng License":
        "ZLIB",
    "Python License (CNRI Python License)":
        "Python",
}

PKGBUILD_HEADER = """\
# Maintainer: {info[maintainer]}

pkgname={pkg.pkgname}
pkgver={pkg.pkgver}
pkgrel=1
pkgdesc={pkg.pkgdesc}
url={pkg.url}
depends=({pkg.depends})
makedepends=({pkg.makedepends})
checkdepends=({pkg.checkdepends})
license=({pkg.license})
arch=({pkg.arch})
"""

SDIST_SOURCE = """\
source=({url[url]})
md5sums=({url[md5_digest]})
"""

WHEEL_ANY_SOURCE = """\
source=({url[url]})
md5sums=({url[md5_digest]})
noextract=({name})
"""

WHEEL_ARCH_SOURCE = """\
source_{arch}=({url[url]})
md5sums_{arch}=({url[md5_digest]})
noextract_{arch}=({name})
"""

MORE_SOURCES = """\
source+=({names})
md5sums+=({md5s})
"""

PKGBUILD_CONTENTS = """\

_PIP_CMD='pip --disable-pip-version-check --isolated'

_first_source() {
    all_sources=("${source_i686[@]}" "${source_x86_64[@]}" "${source[@]}")
    echo ${all_sources[0]}
}

_is_wheel() {
    [[ $(_first_source) =~ \.whl$ ]]
}

_dist_name() {
    dist_name="$(_first_source)"
    for suffix in """ + " ".join(SDIST_SUFFIXES) + """; do
        dist_name="$(basename -s "$suffix" "$dist_name")"
    done
    echo "$dist_name"
}

_license_filename() {
    # See Arch Wiki/PKGBUILD/license.
    if [[ ${license[0]} =~ ^(BSD|MIT|ZLIB|Python)$ ]]; then
        for test_name in """ + " ".join(LICENSE_NAMES) + """; do
            if [[ -e $srcdir/$(_dist_name)/$test_name ]]; then
                echo "$srcdir/$(_dist_name)/$test_name"
                return
            fi
        done
    fi
}

build() {
    _is_wheel && return
    cd "$srcdir/$(_dist_name)"
    $_PIP_CMD wheel -v --no-deps --wheel-dir "$srcdir" .
    license_filename=$(_license_filename)
    if [[ $license_filename ]]; then
        cp "$license_filename" "$srcdir/LICENSE"
    fi
}

check() {
    # Remove the first line line to run tests.
    # You may need to call `python setup.py build_ext -i` first.
    return 0
    _is_wheel && return
    cd "$srcdir/$(_dist_name)"
    python setup.py -q test
}

package() {
    cd "$srcdir"
    # pypa/pip#3063: pip always checks for a globally installed version.
    $_PIP_CMD --quiet install --root="$pkgdir" --no-deps --ignore-installed *.whl
    if [[ -f LICENSE ]]; then
        install -D -m644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
    fi
}

. PKGBUILD_EXTRAS
"""

GITIGNORE = """\
*
!.gitignore
!.SRCINFO
!PKGBUILD
!PKGBUILD_EXTRAS
"""


_run_shell = partial(subprocess.run,
                     shell=True, check=True, universal_newlines=True)


WheelInfo = namedtuple("WheelInfo", "name version py abi platform")
def parse_wheel(fname):
    return WheelInfo(*Path(fname).stem.split("-"))


class NoPackageError(Exception):
    pass


@lru_cache()
def _pypi_request(name):
    try:
        r = urllib.request.urlopen(
            "https://pypi.python.org/pypi/{}/json".format(name))
    except urllib.error.HTTPError:
        raise NoPackageError("Package {!r} not found.".format(name))
    return json.loads(r.read().decode(r.headers.get_param("charset")))


class Package:
    def __init__(self, name, info, prefer_wheel=False):
        stream = StringIO()
        self._files = OrderedDict()

        response = _pypi_request(name)
        self._data = response["info"]
        self._name = name = self._data["name"]  # Normalized.
        self._version = version = self._data["version"]

        LOGGER.info("Packaging %s %s", name, version)
        suffix_prefs = ([WHEEL_SUFFIX, *SDIST_SUFFIXES] if prefer_wheel
                        else [*SDIST_SUFFIXES, WHEEL_SUFFIX])
        urls = sorted(
            self._filter_urls(response["urls"]),
            key=lambda url: next(i for i, suffix in enumerate(suffix_prefs)
                                 if url["path"].endswith(suffix)))
        if not urls:
            raise NoPackageError(
                "No URL available for package {!r}.".format(self._name))
        # Expected to be either a single sdist, or a bunch of wheels.
        self._urls = [
            url for url in urls
            if Path(url["path"]).suffix == Path(urls[0]["path"]).suffix]

        self._find_arch_makedepends_depends()
        self._find_license()

        stream.write(PKGBUILD_HEADER.format(pkg=self, info=info))
        if self._urls[0]["path"].endswith(WHEEL_SUFFIX):
            # Expected to be either just "any", or some specific archs.
            for url in self._urls:
                wheel_info = parse_wheel(url["path"])
                if wheel_info.platform == "any":
                    src_template = WHEEL_ANY_SOURCE
                else:
                    src_template = WHEEL_ARCH_SOURCE
                stream.write(src_template.format(
                    arch=PLATFORM_TAGS[wheel_info.platform],
                    url=url,
                    name=Path(url["path"]).name))
        else:
            stream.write(SDIST_SOURCE.format(url=urls[0]))
        stream.write(MORE_SOURCES.format(
            names=" ".join(shlex.quote(name)
                           for name in self._files),
            md5s=" ".join(hashlib.md5(content).hexdigest()
                          for content in self._files.values())))
        stream.write(PKGBUILD_CONTENTS)

        self._pkgbuild = stream.getvalue()

    def _filter_urls(self, urls):
        for url in urls:
            if url["path"].endswith(WHEEL_SUFFIX):
                wheel_info = parse_wheel(url["path"])
                assert (wheel_info.name == self._name.replace("-", "_")
                        and wheel_info.version == self._version)
                if (wheel_info.py not in PY_TAGS
                        or wheel_info.platform not in PLATFORM_TAGS):
                    continue
                yield url
            elif url["path"].endswith(tuple(SDIST_SUFFIXES)):
                yield url
            else:
                LOGGER.warning("Skipping unknown suffix: %s",
                               Path(url["path"]).name)

    def _find_arch_makedepends_depends(self):
        self._arch = ["any"]
        self._makedepends = ["python-pip"]
        with TemporaryDirectory() as tmpdir:
            if self._urls[0]["path"].endswith(WHEEL_SUFFIX):
                self._arch = sorted(
                    {PLATFORM_TAGS[parse_wheel(url["path"]).platform]
                     for url in self._urls})
            else:
                r = urllib.request.urlopen(self._urls[0]["url"])
                tmppath = Path(tmpdir, Path(self._urls[0]["path"]).name)
                tmppath.write_bytes(r.read())
                shutil.unpack_archive(str(tmppath), tmpdir)
                if list(Path(tmpdir).glob("**/*.pyx")):
                    self._arch = ["i686", "x86_64"]
                    self._makedepends += ["cython"]
                if list(Path(tmpdir).glob("**/*.c")):
                    self._arch = ["i686", "x86_64"]

        # Dependency resolution is done by installing the package in a venv
        # and calling `pip show`; otherwise it would be necessary to parse
        # environment markers (from `self._data["requires_dist"]`).
        # The package name may get denormalized ("_" -> "-") during installation
        # so we just look at whatever got installed.
        with TemporaryDirectory() as venvdir:
            script = (r"""
                pyvenv {venvdir}
                . {venvdir}/bin/activate
                pip --isolated install --upgrade pip >/dev/null
                {install_cython}
                pip --isolated install --no-deps {self._name} \
                    >/dev/null
                pip show "$(pip freeze | cut -d= -f1 | grep -v '^Cython$')" \
                    | grep -Po '(?<=^Requires: ).*'
            """.format(
                self=self,
                venvdir=venvdir,
                install_cython=("pip --isolated install cython >/dev/null"
                                if "cython" in self._makedepends
                                else "")))
            process = _run_shell(["sh"], input=script, stdout=PIPE)
        self._depends = (  # Normalize names.
            ["python-{}".format(_pypi_request(depend)["info"]["name"])
             for depend in filter(
                     None, process.stdout[:-1].split(", "))]  # Strip newline.
            or ["python"]) # In case there are no other dependencies.

    def _find_license(self):
        license_classes = [
            classifier for classifier in self._data["classifiers"]
            if classifier.startswith("License :: ")]
        if len(license_classes) > 1:
            raise ValueError("Multiple licenses not supported")
        elif len(license_classes) == 1:
            license_class = license_classes[0].split(" :: ")[-1]
            try:
                self._license = {**TROVE_COMMON_LICENSES,
                                 **TROVE_SPECIAL_LICENSES}[license_class]
            except KeyError:
                self._license = license_class
        elif self._data["license"] not in [None, "UNKNOWN"]:
            self._license = self._data["license"]
        else:
            raise ValueError("No license information available")

        if self._license not in TROVE_COMMON_LICENSES:
            url, subbed = re.subn(
                r"https?://(www\.)?github\.com",
                "https://raw.githubusercontent.com",
                self._data["download_url"] or self._data["home_page"],
                1)
            if subbed:
                for license_name in LICENSE_NAMES:
                    try:
                        r = urllib.request.urlopen(
                            url + "/master/" + license_name)
                    except urllib.error.HTTPError:
                        pass
                    else:
                        self._files.update(LICENSE=r.read())
            else:
                LOGGER.warning("Could not retrieve license file")

    pkgname = property(lambda self: "python-{self._name}".format(self=self))
    pkgver = property(lambda self: shlex.quote(self._version))
    pkgdesc = property(lambda self: shlex.quote(self._data["summary"]))
    url = property(lambda self: shlex.quote(self._data["home_page"]))
    depends = property(lambda self: " ".join(self._depends))
    makedepends = property(lambda self: " ".join(self._makedepends))
    checkdepends = property(lambda self: "")
    license = property(lambda self: shlex.quote(self._license))
    arch = property(lambda self: " ".join(self._arch))

    def get_pkgbuild_contents(self):
        return self._pkgbuild

    def get_files(self):
        return self._files


def get_config():
    with TemporaryDirectory() as tmpdir:
        mini_pkgbuild = ('pkgver=0\npkgrel=0\narch=(any)\n'
                         'prepare() { echo "$PACKAGER"; exit 0; }')
        Path(tmpdir, "PKGBUILD").write_text(mini_pkgbuild)
        maintainer = (
            _run_shell("makepkg", cwd=tmpdir, stdout=PIPE, stderr=PIPE).
            stdout[:-1])  # Strip newline.
    return {"maintainer": maintainer}


def main(name,
         force=False, prefer_wheel=False, makepkg="--cleanbuild --nodeps"):

    package = Package(name, get_config(), prefer_wheel=prefer_wheel)

    for dep in package.depends.split():
        if not dep.startswith("python-"):
            continue
        dep = dep[len("python-"):]
        process = subprocess.run(
            ["pkgfile", "-r", r"/{0}-.*py{1.major}.{1.minor}\.egg-info".format(
                dep, sys.version_info)], stdout=PIPE)
        if process.returncode:
            # Dependency not found, build it too.
            main(dep, force=force, prefer_wheel=prefer_wheel, makepkg=makepkg)

    cwd = package.pkgname
    Path(cwd).mkdir(parents=True, exist_ok=force)
    _run_shell("git init .", cwd=cwd)
    Path(cwd, ".gitignore").write_text(GITIGNORE)
    Path(cwd, "PKGBUILD_EXTRAS").open("a").close()
    Path(cwd, "PKGBUILD").write_text(package.get_pkgbuild_contents())
    for fname, content in package.get_files().items():
        Path(cwd, fname).write_bytes(content)
    _run_shell("mksrcinfo", cwd=cwd)
    cmd = ["makepkg",
           *(["--force"] if force else []),
           *shlex.split(makepkg)]
    subprocess.run(cmd, check=True, cwd=cwd)
    _run_shell("namcap PKGBUILD", cwd=cwd)
    fnames = (_run_shell("makepkg --packagelist", cwd=cwd, stdout=PIPE).
              stdout.splitlines())
    for fname in fnames:
        for fullname in Path(cwd).glob(fname + ".*"):
            # Python dependencies always get misanalyzed so we just filter them
            # away; how to do this via a switch to namcap is not so clear.
            _run_shell(
                "namcap {} | grep -v 'W: Dependency included and not needed' "
                "|| true".format(fullname.name),
                cwd=cwd)


if __name__ == "__main__":
    logging.basicConfig(level="INFO")
    parser = ArgumentParser(
        description="Create a PKGBUILD for a PyPI package and run makepkg.",
        formatter_class=ArgumentDefaultsHelpFormatter)
    parser.add_argument("name", help="The PyPI package name.")
    parser.add_argument("-f", "--force", action="store_true",
                        help="Overwrite a previously existing PKGBUILD.")
    parser.add_argument("-w", "--prefer-wheel", action="store_true",
                        help="Prefer wheels to sdists.")
    parser.add_argument("-m", "--makepkg", default="--cleanbuild --nodeps",
                        help="Additional arguments to pass to makepkg.")
    args = parser.parse_args()
    try:
        main(**vars(args))
    except NoPackageError as e:
        print(e, file=sys.stderr)
        sys.exit(1)
