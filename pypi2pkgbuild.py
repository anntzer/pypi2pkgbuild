#!/usr/bin/env python
from argparse import ArgumentParser, ArgumentDefaultsHelpFormatter
from collections import namedtuple, OrderedDict
from contextlib import suppress
from functools import lru_cache, partial
import hashlib
from io import StringIO
import json
import logging
from pathlib import Path
import shlex
import shutil
import subprocess
from subprocess import CalledProcessError, DEVNULL, PIPE
import sys
from tempfile import TemporaryDirectory
import urllib.request


LOGGER = logging.getLogger(Path(__file__).stem)

PY_TAGS = ["py2.py3",
           "py{0.major}".format(sys.version_info),
           "cp{0.major}{0.minor}".format(sys.version_info)]
PLATFORM_TAGS = {
    "any": "any", "manylinux1_i686": "i686", "manylinux1_x86_64": "x86_64"}
SDIST_SUFFIXES = [".tar.gz", ".tgz", ".tar.bz2", ".zip"]
LICENSE_NAMES = [
    "LICENSE", "LICENSE.txt", "LICENCE", "LICENCE.txt", "license.txt",
    "COPYING.rst", "COPYING.txt", "COPYRIGHT"]
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
    "Mozilla Public License 2.0 (MPL 2.0)":
        # Technically different, but Arch itself marks e.g. Firefox as "MPL".
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
pkgrel=00
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
## EXTRA_DEPENDS ##

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
    return json.loads(r.read().decode(r.headers.get_param("charset")),
                      object_pairs_hook=OrderedDict)


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
    def __init__(self, name, info, prefer):
        stream = StringIO()
        self._srctree = None
        self._files = OrderedDict()

        self._ref = PackageRef(name)

        response = _pypi_request(name)
        self._data = response["info"]
        self._version = version = self._data["version"]

        LOGGER.info("Packaging %s %s", self.pkgname, version)
        self._urls = self._filter_and_sort_urls(response["urls"], prefer)
        if not self._urls:
            raise NoPackageError(
                "No URL available for package {!r}.".format(self.pkgname))

        self._find_arch_makedepends_depends()
        self._find_license()

        stream.write(PKGBUILD_HEADER.format(pkg=self, info=info))
        if self._urls[0]["packagetype"] == "bdist_wheel":
            # Either just "any", or some specific archs.
            for url in self._urls:
                if url["packagetype"] != "bdist_wheel":
                    continue
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
            stream.write(SDIST_SOURCE.format(url=self._urls[0]))
        stream.write(MORE_SOURCES.format(
            names=" ".join(shlex.quote(name)
                           for name in self._files),
            md5s=" ".join(hashlib.md5(content).hexdigest()
                          for content in self._files.values())))
        stream.write(PKGBUILD_CONTENTS)

        self._pkgbuild = stream.getvalue()

    def _filter_and_sort_urls(self, unfiltered_urls, prefer):
        urls = []
        for url in unfiltered_urls:
            if url["packagetype"] == "bdist_wheel":
                wheel_info = parse_wheel(url["path"])
                assert (wheel_info.name == self._ref.wheel_name
                        and wheel_info.version == self._version)
                if wheel_info.py not in PY_TAGS:
                    continue
                if wheel_info.platform == "any":
                    with suppress(ValueError):
                        urls.append((url, prefer.index("anywheel")))
                elif wheel_info.platform.startswith("manylinux"):
                    with suppress(ValueError):
                        urls.append((url, prefer.index("manylinuxwheel")))
                else:  # Skip other platforms.
                    continue
            elif url["packagetype"] == "sdist":
                with suppress(ValueError):
                    urls.append((url, prefer.index("sdist")))
            else:  # Skip other dists.
                continue
        return [url for url, key in sorted(urls, key=lambda kv: kv[1])]

    def _get_srctree(self):
        url = next(url for url in self._urls
                   if url["path"].endswith(tuple(SDIST_SUFFIXES)))
        if self._srctree is None:
            self._srctree = TemporaryDirectory()
            r = urllib.request.urlopen(url["url"])
            tmppath = Path(self._srctree.name, Path(url["path"]).name)
            tmppath.write_bytes(r.read())
            shutil.unpack_archive(str(tmppath), self._srctree.name)
        return Path(self._srctree.name,
                    "{0._ref.pypi_name}-{0.pkgver}".format(self))

    def _find_arch_makedepends_depends(self):
        self._arch = ["any"]
        self._makedepends = PackageRefList([PackageRef("pip")])
        makedepends_cython = False
        archs = sorted(
            {PLATFORM_TAGS[parse_wheel(url["path"]).platform]
             for url in self._urls if url["packagetype"] == "bdist_wheel"})
        if self._urls[0]["packagetype"] == "bdist_wheel":
            self._arch = archs
        else:
            if list(self._get_srctree().glob("**/*.pyx")):
                self._arch = ["i686", "x86_64"]
                self._makedepends.append(PackageRef("cython"))
                makedepends_cython = True
            if not "any" in archs and list(self._get_srctree().glob("**/*.c")):
                # Don't bother checking for the presence of C sources if
                # there's an "any" wheel available; e.g. pexpect has a C source
                # in its *tests*.
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
        self._licenses = licenses = []
        license_classes = [
            classifier for classifier in self._data["classifiers"]
            if classifier.startswith("License :: ")
               and classifier != "License :: OSI Approved"]  # What's that?...
        if license_classes:
            for license_class in license_classes:
                *_, license_class = license_class.split(" :: ")
                try:
                    licenses.append(
                        {**TROVE_COMMON_LICENSES,
                         **TROVE_SPECIAL_LICENSES}[license_class])
                except KeyError:
                    licenses.append("custom:{}".format(license_class))
        elif self._data["license"] not in [None, "UNKNOWN"]:
            licenses.append("custom:{}".format(self._data["license"]))
        else:
            LOGGER.warning("No license information available")
            licenses.append("custom:unknown")

        _license_found = False
        if any(license not in TROVE_COMMON_LICENSES for license in licenses):
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
                for path in (self._get_srctree() / license_name
                             for license_name in LICENSE_NAMES):
                    if path.exists():
                        self._files.update(LICENSE=path.read_bytes())
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
    license = property(lambda self: " ".join(map(shlex.quote, self._licenses)))
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


def create_package(name,
                   force=False,
                   prefer=False,
                   skipdeps=False,
                   makepkg="--cleanbuild --nodeps"):

    package = Package(name, get_config(), prefer=prefer)

    if not skipdeps:
        for ref in package._depends:
            if not ref.exists:
                # Dependency not found, build it too.
                create_package(
                    ref.pypi_name, force=force, prefer=prefer, makepkg=makepkg)

    cwd = package.pkgname
    Path(cwd).mkdir(parents=True, exist_ok=force)
    _run_shell("git init .", cwd=cwd)
    Path(cwd, ".gitignore").write_text(GITIGNORE)
    Path(cwd, "PKGBUILD_EXTRAS").open("a").close()
    Path(cwd, "PKGBUILD").write_text(package.get_pkgbuild_contents())
    for fname, content in package.get_files().items():
        Path(cwd, fname).write_bytes(content)
    cmd = ["makepkg",
           *(["--force"] if force else []),
           *shlex.split(makepkg)]
    subprocess.run(cmd, check=True, cwd=cwd)
    # Only one of the archs will be globbed successfully.
    fullname, = sum(
        (list(Path(cwd).glob(fname + ".*"))
         for fname in (_run_shell("makepkg --packagelist", cwd=cwd, stdout=PIPE).
                       stdout.splitlines())),
        [])
    extra_deps = _run_shell(
        "namcap {} | "
        "grep -Po '(?<=E: Dependency ).*(?= detected and not included)'"
        "|| true".format(fullname.name),
        cwd=cwd, stdout=PIPE).stdout.splitlines()
    Path(cwd, "PKGBUILD").write_text(
        package.get_pkgbuild_contents().replace(
            "## EXTRA_DEPENDS ##",
            "depends+=({})".format(" ".join(extra_deps))))
    _run_shell("makepkg --force --repackage --nodeps", cwd=cwd)
    # Python dependencies always get misanalyzed so we just filter them
    # away; how to do this via a switch to namcap is not so clear.
    _run_shell(
        "namcap {} | grep -v 'W: Dependency included and not needed' || true".
        format(fullname.name),
        cwd=cwd)
    _run_shell("namcap PKGBUILD", cwd=cwd)
    _run_shell("makepkg --printsrcinfo >.SRCINFO", cwd=cwd)


def find_outdated():
    syswide_location = (
        "{0.prefix}/lib/python{0.version_info.major}.{0.version_info.minor}"
        "/site-packages".format(sys))
    # `pip show` is rather slow, so just call it once.
    lines = _run_shell("pip list --outdated", stdout=PIPE).stdout.splitlines()
    names = [line.split()[0] for line in lines]
    locs = _run_shell("pip show {} | grep -Po '(?<=^Location: ).*'".
                      format(" ".join(names)), stdout=PIPE).stdout.splitlines()
    for line, name, loc in zip(lines, names, locs):
        if loc == syswide_location:
            if _run_shell(
                    "pacman -Qo {}/{}-*".format(
                        syswide_location, name.replace("-", "_")),
                    stdout=PIPE).stdout[:-1].endswith("-00"):
                print(line)


def main():
    try:
        _run_shell("pkgfile pkgfile", stdout=DEVNULL)
    except CalledProcessError:
        # Display one of:
        #   - "/bin/sh: pkgfile: command not found"
        #   - "error: No repo files found. Please run `pkgfile --update'."
        # on stderr.
        sys.exit(1)

    logging.basicConfig(level="INFO")
    parser = ArgumentParser(
        description="Create a PKGBUILD for a PyPI package and run makepkg.",
        formatter_class=ArgumentDefaultsHelpFormatter)
    parser.add_argument(
        "name", nargs="?",
        help="The PyPI package name.")
    parser.add_argument(
        "-o", "--outdated", action="store_true",
        help="Find outdated automatic packages.")
    parser.add_argument(
        "-f", "--force", action="store_true",
        help="Overwrite a previously existing PKGBUILD.")
    parser.add_argument(
        "-p", "--prefer", metavar="P",
        default="anywheel:sdist:manylinuxwheel",
        type=partial(str.split, sep=":"),
        help="Preference order for dists.")
    parser.add_argument(
        "-s", "--skipdeps", action="store_true",
        help="Don't generate PKGBUILD for dependencies.")
    parser.add_argument(
        "-m", "--makepkg", metavar="M",
        default="--cleanbuild --nodeps",
        help="Additional arguments to pass to makepkg.")
    args = parser.parse_args()

    if args.outdated:
        if args.name:
            parser.error("--outdated should be given alone.")
        else:
            find_outdated()

    else:
        del args.outdated
        if not args.name:
            parser.error("error: the following arguments are required: name")
        try:
            create_package(**vars(args))
        except NoPackageError as e:
            print(e, file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
