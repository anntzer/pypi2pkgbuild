#!/usr/bin/env python
from argparse import ArgumentParser, ArgumentDefaultsHelpFormatter
from collections import namedtuple, OrderedDict
from functools import lru_cache, partial
import hashlib
from io import StringIO
import json
import logging
from pathlib import Path
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
LICENSE_NAMES = [
    "LICENSE", "LICENSE.txt", "COPYING.rst", "COPYING.txt", "COPYRIGHT"]
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
    "GNU General Public License (GPL)":
        "GPL",
    "GNU General Public License v2 (GPLv2)":
        "GPL2",
    "GNU General Public License v2 or later (GPLv2+)":
        "GPL2",
    "GNU General Public License v3 (GPLv3)":
        "GPL3",
    "GNU General Public License v3 or later (GPLv3+)":
        "GPL3",
    "GNU Library or Lesser General Public License (LGPL)":
        "LGPL",
    "GNU Lesser General Public License v2 (LGPLv2)":
        "LGPL2.1",
    "GNU Lesser General Public License v2 or later (LGPLv2+)":
        "LGPL2.1",
    "GNU Lesser General Public License v3 (LGPLv3)":
        "LGPL3",
    "GNU Lesser General Public License v3 or later (LGPLv3+)":
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
pkgrel=0
pkgdesc={pkg.pkgdesc}
url={pkg.url}
depends=(python {pkg.depends})
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


class PackageRef:
    def __init__(self, name):
        # Name on PyPI.
        self.pypi_name = _pypi_request(name)["info"]["name"]
        # Name for wheels.
        self.wheel_name = self.pypi_name.replace("-", "_")
        # Name for Arch Linux.
        process = subprocess.run(
            ["pkgfile", "-rq", r"/{0}-.*py{1.major}.{1.minor}\.egg-info".format(
                self.pypi_name, sys.version_info)],
            stdout=PIPE, universal_newlines=True)
        if process.returncode:
            self.pkgname = "python-{}".format(self.pypi_name.lower())
            self.exists = False
        else:
            self.pkgname = process.stdout[:-1]  # Strip newline.
            self.exists = True


class PackageRefList(list):
    def __format__(self, fmt):
        if fmt == "":
            return " ".join(ref.pkgname for ref in self)
        return super().__format__(fmt)  # Raise TypeError.


class Package:
    def __init__(self, name, info, prefer_wheel=False):
        stream = StringIO()
        self._files = OrderedDict()

        self._ref = PackageRef(name)

        response = _pypi_request(name)
        self._data = response["info"]
        self._version = version = self._data["version"]

        LOGGER.info("Packaging %s %s", self.pkgname, version)
        type_prefs = (["bdist_wheel", "sdist"] if prefer_wheel
                      else ["sdist", "bdist_wheel"])
        urls = sorted(
            self._filter_urls(response["urls"]),
            key=lambda url: type_prefs.index(url["packagetype"]))
        if not urls:
            raise NoPackageError(
                "No URL available for package {!r}.".format(self.pkgname))
        # Expected to be either a single sdist, or a bunch of wheels.
        self._urls = [
            url for url in urls
            if Path(url["path"]).suffix == Path(urls[0]["path"]).suffix]

        self._find_arch_makedepends_depends()
        self._find_license()

        stream.write(PKGBUILD_HEADER.format(pkg=self, info=info))
        if self._urls[0]["packagetype"] == "bdist_wheel":
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
            if url["packagetype"] == "bdist_wheel":
                wheel_info = parse_wheel(url["path"])
                assert (wheel_info.name == self._ref.wheel_name
                        and wheel_info.version == self._version)
                if (wheel_info.py not in PY_TAGS
                        or wheel_info.platform not in PLATFORM_TAGS):
                    continue
                yield url
            elif url["packagetype"] == "sdist":
                yield url
            # Skip other dists.

    def _find_arch_makedepends_depends(self):
        self._arch = ["any"]
        self._makedepends = PackageRefList([PackageRef("pip")])
        makedepends_cython = False
        with TemporaryDirectory() as tmpdir:
            if self._urls[0]["packagetype"] == "bdist_wheel":
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
                    self._makedepends.append(PackageRef("cython"))
                    makedepends_cython = True
                if list(Path(tmpdir).glob("**/*.c")):
                    self._arch = ["i686", "x86_64"]

        # Dependency resolution is done by installing the package in a venv
        # and calling `pip show`; otherwise it would be necessary to parse
        # environment markers (from `self._data["requires_dist"]`).
        # The package name may get denormalized ("_" -> "-") during installation
        # so we just look at whatever got installed.
        #
        # To handle sdists that depend on numpy, we just see whether installing
        # in presence of numpy makes things better...
        with TemporaryDirectory() as venvdir:
            script = (r"""
            pyvenv {venvdir}
            . {venvdir}/bin/activate
            pip --isolated install --upgrade pip >/dev/null
            {install_cython}
            INSTALL_CMD='pip --isolated install --no-deps {self._ref.pypi_name}'
            $INSTALL_CMD >/dev/null \
                || (echo 'numpy' \
                    && pip --isolated install --no-deps numpy >/dev/null \
                    && $INSTALL_CMD >/dev/null)
            pip show "$(pip freeze | cut -d= -f1 | grep -v '^Cython\|numpy$')" \
                | grep -Po '(?<=^Requires:).*'
            """.format(
                self=self,
                venvdir=venvdir,
                install_cython=("pip --isolated install cython >/dev/null"
                                if makedepends_cython
                                else "")))
            process = _run_shell(["sh"], input=script, stdout=PIPE)
        depends = process.stdout[:-1].replace(",", " ").split()
        depends = list(  # Drop prefix duplicates.
            OrderedDict(zip(depends[::-1], [None] * len(depends))))[::-1]
        self._depends = PackageRefList(PackageRef(depend) for depend in depends)

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
            LOGGER.warning("No license information available")
            self._license = "UNKNOWN"

        _license_found = False
        if self._license not in TROVE_COMMON_LICENSES:
            for url in [self._data["download_url"], self._data["home_page"]]:
                parse = urllib.parse.urlparse(url or "")  # Could be None.
                if len(Path(parse.path).parts) != 3:  # ["/", user, name]
                    continue
                if parse.netloc in ["github.com", "www.github.com"]:
                    url = urllib.parse.urlunparse(parse._replace(
                        netloc="raw.githubusercontent.com"))
                elif parse.netloc in ["bitbucket.org", "www.bitbucket.org"]:
                    url += "/raw"
                else:
                    continue
                for license_name in LICENSE_NAMES:
                    try:
                        r = urllib.request.urlopen(
                            url + "/master/" + license_name)
                    except urllib.error.HTTPError:
                        pass
                    else:
                        self._files.update(LICENSE=r.read())
                        _license_found = True
                        break
                if _license_found:
                    break
            else:
                LOGGER.warning("Could not retrieve license file")

    pkgname = property(lambda self: self._ref.pkgname)
    pkgver = property(lambda self: shlex.quote(self._version))
    pkgdesc = property(lambda self: shlex.quote(self._data["summary"]))
    url = property(lambda self: shlex.quote(
        next(url for url in [self._data["home_page"],
                             self._data["download_url"],
                             self._data["package_url"]]
             if url not in [None, "UNKNOWN"])))
    depends = property(lambda self: self._depends)
    makedepends = property(lambda self: self._makedepends)
    checkdepends = property(lambda self: PackageRefList())
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
         force=False,
         prefer_wheel=False,
         skipdeps=False,
         makepkg="--cleanbuild --nodeps"):

    package = Package(name, get_config(), prefer_wheel=prefer_wheel)

    if not skipdeps:
        for ref in package._depends:
            if not ref.exists:
                # Dependency not found, build it too.
                main(ref.pypi_name,
                     force=force, prefer_wheel=prefer_wheel, makepkg=makepkg)

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
    parser.add_argument("-s", "--skipdeps", action="store_true",
                        help="Don't generate PKGBUILD for dependencies.")
    parser.add_argument("-m", "--makepkg", default="--cleanbuild --nodeps",
                        help="Additional arguments to pass to makepkg.")
    args = parser.parse_args()
    try:
        main(**vars(args))
    except NoPackageError as e:
        print(e, file=sys.stderr)
        sys.exit(1)
