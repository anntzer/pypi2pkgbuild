#!/usr/bin/env python
import abc
from abc import ABC
from argparse import (ArgumentParser, ArgumentDefaultsHelpFormatter,
                      RawDescriptionHelpFormatter)
from collections import namedtuple, OrderedDict
from contextlib import suppress
from functools import lru_cache
import hashlib
from io import StringIO
from itertools import repeat
import json
import logging
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
from subprocess import CalledProcessError, DEVNULL, PIPE
import sys
from tempfile import NamedTemporaryFile, TemporaryDirectory
import urllib.request


LOGGER = logging.getLogger(Path(__file__).stem)

PY_TAGS = ["py2.py3",
           "py{0.major}".format(sys.version_info),
           "cp{0.major}{0.minor}".format(sys.version_info)]
PLATFORM_TAGS = {
    "any": "any", "manylinux1_i686": "i686", "manylinux1_x86_64": "x86_64"}
THIS_ARCH = ["i686", "x86_64"][sys.maxsize > 2 ** 32]
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
# Maintainer: {config[maintainer]}

pkgname={pkg.pkgname}
epoch={pkg.epoch}
pkgver={pkg.pkgver}
pkgrel={pkg.pkgrel}
pkgdesc={pkg.pkgdesc}
arch=({pkg.arch})
url={pkg.url}
license=({pkg.license})
depends=(python {pkg.depends})
makedepends=({pkg.makedepends})
checkdepends=({pkg.checkdepends})
provides=()
conflicts=()
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
provides+=($(if [[ ${source[0]} =~ ^git+ ]]; then echo "$pkgname" | sed 's/-git$//'; fi))
conflicts+=($(if [[ ${source[0]} =~ ^git+ ]]; then echo "$pkgname" | sed 's/-git$//'; fi))

export PIP_CONFIG_FILE=/dev/null
export PIP_DISABLE_PIP_VERSION_CHECK=true

_first_source() {
    all_sources=("${source_i686[@]}" "${source_x86_64[@]}" "${source[@]}")
    echo ${all_sources[0]}
}

_is_wheel() {
    [[ $(_first_source) =~ \\.whl$ ]]
}

_dist_name() {
    dist_name="$(_first_source)"
    for suffix in """ + " ".join(SDIST_SUFFIXES) + """ .git; do
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

if [[ $(_first_source) =~ ^git+ ]]; then
    pkgver() {
        ( set -o pipefail
          cd "$srcdir/$(_dist_name)"
          git describe --long --tags 2>/dev/null |
            sed 's/^v//;s/\\([^-]*-g\\)/r\\1/;s/-/./g' ||
          printf "r%s.%s" "$(git rev-list --count HEAD)" "$(git rev-parse --short HEAD)"
        )
    }
fi

build() {
    _is_wheel && return
    cd "$srcdir/$(_dist_name)"
    pip wheel -v --no-deps --wheel-dir "$srcdir" .
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
    pip --quiet install --root="$pkgdir" --no-deps --ignore-installed *.whl
    if [[ -f LICENSE ]]; then
        install -D -m644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
    fi
}

. "$(dirname "$BASH_SOURCE")/PKGBUILD_EXTRAS"
"""

MULTIPKGBUILD_CONTENTS = """\
package() {
    true
}
"""

GITIGNORE = """\
*
!.gitignore
!.SRCINFO
!PKGBUILD
!PKGBUILD_EXTRAS
"""


def _unique(seq):
    """Return unique elements in a sequence, keeping them in order.
    """
    return list(OrderedDict(zip(list(seq)[::-1], repeat(None))))[::-1]


def _subprocess_run(argv, *args, **kwargs):
    """Logging wrapper for `subprocess.run`.

    Log at `DEBUG` level except if the `verbose` kwarg is set, in which case
    log at `INFO` level.
    """
    level = logging.INFO if kwargs.pop("verbose", None) else logging.DEBUG
    argv_s = (" ".join(map(shlex.quote, argv)) if isinstance(argv, list)
              else argv)
    if "cwd" in kwargs:
        LOGGER.log(level,
                   "Running subprocess from %s:\n%s", kwargs["cwd"], argv_s)
    else:
        LOGGER.log(level, "Running subprocess:\n%s", argv_s)
    return subprocess.run(argv, *args, **kwargs)


def _run_shell(*args, **kwargs):
    """`_subprocess_run` with useful defaults.
    """
    kwargs = {"shell": True, "check": True, "universal_newlines": True,
              **kwargs}
    if "cwd" in kwargs:
        kwargs["cwd"] = str(Path(kwargs["cwd"]))
    return _subprocess_run(*args, **kwargs)


class ArchVersion(namedtuple("_ArchVersion", "epoch pkgver pkgrel")):
    def __str__(self):
        return ("{0.epoch}:{0.pkgver}-{0.pkgrel}" if self.epoch
                else "{0.pkgver}-{0.pkgrel}").format(self)


WheelInfo = namedtuple("WheelInfo", "name version py abi platform")
def parse_wheel(fname):
    return WheelInfo(*Path(fname).stem.split("-"))


class PackagingError(Exception):
    pass


@lru_cache()
def _get_metadata(name, makedepends_cython):
    # Dependency resolution is done by installing the package in a venv and
    # calling `pip show`; otherwise it would be necessary to parse environment
    # markers (from "requires_dist").  The package name may get denormalized
    # ("_" -> "-") during installation so we just look at whatever got
    # installed.
    #
    # `entry_points` is a generator, thus not json-serializable.
    #
    # To handle sdists that depend on numpy, we just see whether installing in
    # presence of numpy makes things better...
    with TemporaryDirectory() as venvdir, \
            NamedTemporaryFile("r") as more_requires, \
            NamedTemporaryFile("r") as log:
        script = (r"""
        pyvenv {venvdir}
        . {venvdir}/bin/activate
        export PIP_CONFIG_FILE=/dev/null
        pip install --upgrade pip >/dev/null
        {install_cython}
        install_cmd() {{
            pip install --no-deps {name}
        }}
        show_cmd() {{
            # known packages that must be excluded.
            if [[ {name} =~ ^setuptools|pip|Cython|numpy$ ]]; then
                name={name}
            else
                name="$(pip freeze | cut -d= -f1 | grep -v '^Cython\|numpy$')"
            fi
            python -c \
                "import json, pip; info = next(pip.commands.show.search_packages_info(['$name'])); info.pop('entry_points', None); print(json.dumps(info))"
        }}
        if install_cmd >/dev/null; then
            show_cmd
        else
            pip install numpy >/dev/null
            echo numpy >>{more_requires.name}
            install_cmd >{log.name}
            show_cmd
        fi
        """.format(
            name={"setuptools": "setuptools", "pip": "pip",
                  "cython": "Cython", "numpy": "numpy"}.get(
                      name.lower(), name.lower()),
            venvdir=venvdir,
            more_requires=more_requires,
            log=log,
            install_cython=("pip install cython >/dev/null"
                            if makedepends_cython
                            else "")))
        try:
            process = _run_shell(script, stdout=PIPE)
        except CalledProcessError:
            print(log.read(), file=sys.stderr)
            raise PackagingError(
                "Failed to obtain metadata for {!r}.".format(name))
        more_requires = more_requires.read().splitlines()
    metadata = json.loads(process.stdout)
    metadata["requires"].extend(more_requires)
    return {key.replace("-", "_"): value for key, value in metadata.items()}


@lru_cache()
def _get_pypi_info(name):
    if name.startswith("git+"):
        metadata = _get_metadata(name, True)
        try:  # Normalize the name if available on PyPI.
            metadata["name"] = _get_pypi_info(metadata["name"])["info"]["name"]
        except PackagingError:
            pass
        return {"info": {"download_url": name[4:],  # Strip "git+".
                         "home_page": name[4:],
                         "package_url": name[4:],
                         **metadata},
                "urls": [{"packagetype": "sdist",
                          "path": urllib.parse.urlparse(name).path,
                          "url": name,
                          "md5_digest": "SKIP"}],
                "_pkgname_suffix": "-git"}
    else:
        try:
            r = urllib.request.urlopen(
                "https://pypi.python.org/pypi/{}/json".format(name))
        except urllib.error.HTTPError:
            raise PackagingError("Package {!r} not found.".format(name))
        # Load as OrderedDict so that always the same sdist is chosen if e.g.
        # both zip and tgz are available.
        request = json.loads(r.read().decode(r.headers.get_param("charset")),
                            object_pairs_hook=OrderedDict)
        request["_pkgname_suffix"] = ""
        return request


class NonPyPackageRef:
    def __init__(self, pkgname):
        self.pkgname = pkgname


class PackageRef:
    def __init__(self, name, *, force_new=False):
        # If `force_new` is set, do not attempt to use the Arch Linux name.
        self.orig_name = name  # A name or an URL.
        self.info = _get_pypi_info(name)
        self.pypi_name = self.info["info"]["name"] # Name on PyPI.
        self.wheel_name = self.pypi_name.replace("-", "_") # Name for wheels.
        # Name for Arch Linux.  Different cases may be used for pip and PyPI
        # (e.g. "cycler" is "Cycler" on PyPI) so just ignore it.
        # FIXME Also call pacman -Qo to check if the package has been installed
        # from the AUR (see e.g. pipdeptree)?  (At least print a warning?)
        process = _run_shell(
            r"pkgfile -riv '/{0}-.*py{1.major}\.{1.minor}\.egg-info' "
            "| cut -f1 | uniq".format(
                self.wheel_name, sys.version_info),
            stdout=PIPE)
        if force_new or not process.stdout:
            self.pkgname = "python-{}{}".format(
                self.pypi_name.lower(), self.info["_pkgname_suffix"])
            self.arch_version = None
            self.arch_packaged = []
            self.exists = False
        else:
            self.pkgname = pkgname = process.stdout[:-1]  # Strip newline.
            self.pkgname, epoch, pkgver, pkgrel = (
                re.fullmatch(r"[\w-]+/([\w-]+) (?:(.*):)?(.*)-(.*)\n",
                             process.stdout).groups())
            self.arch_version = ArchVersion(epoch or "", pkgver, pkgrel)
            packaged = _run_shell(
                "pkgfile -l {} "
                "| grep -Po '(?<=site-packages/)[^-]*(?=.*\.egg-info/?$)'".
                format(pkgname), stdout=PIPE).stdout.splitlines()
            self.arch_packaged = packaged
            self.exists = True


class PackageRefList(list):
    def __format__(self, fmt):
        if fmt == "":
            return " ".join(_unique(ref.pkgname for ref in self))
        return super().__format__(fmt)  # Raise TypeError.


class _BasePackage(ABC):
    build_cache = []

    def __init__(self):
        self._files = OrderedDict()
        # self._pkgbuild = ...

    @abc.abstractmethod
    def write_deps_to(self, base_path, *, force, prefer, makepkg):
        pass

    def write_to(self, base_path, *, force, makepkg):
        cwd = base_path / self.pkgname
        cwd.mkdir(parents=True, exist_ok=force)
        _run_shell("git init .", cwd=cwd)
        (cwd / ".gitignore").write_text(GITIGNORE)
        (cwd / "PKGBUILD_EXTRAS").open("a").close()
        (cwd / "PKGBUILD").write_text(self._pkgbuild)
        for fname, content in self._files.items():
            (cwd / fname).write_bytes(content)
        cmd = ["makepkg",
               *(["--force"] if force else []),
               *shlex.split(makepkg)]
        _subprocess_run(cmd, check=True, cwd=str(cwd))

        def _get_fullname():
            # Only one of the archs will be globbed successfully.
            fullname, = sum(
                (list(cwd.glob(fname + ".*"))
                for fname in (
                    _run_shell("makepkg --packagelist", cwd=cwd, stdout=PIPE).
                    stdout.splitlines())),
                [])
            return fullname

        # Update PKGBUILD.
        namcap = (
            _subprocess_run(
                ["namcap", _get_fullname().name],
                cwd=str(cwd), stdout=PIPE, universal_newlines=True)
            .stdout.splitlines())
        # `pkgver()` may update the PKGBUILD, so reread it.
        pkgbuild_contents = (cwd / "PKGBUILD").read_text()
        # Binary dependencies.
        extra_deps_re = "(?<=E: Dependency ).*(?= detected and not included)"
        extra_deps = [
            match.group(0)
            for match in filter(None, (re.search(extra_deps_re, line)
                                for line in namcap))]
        pkgbuild_contents = pkgbuild_contents.replace(
            "## EXTRA_DEPENDS ##",
            "depends+=({})".format(" ".join(extra_deps)))
        # Unexpected archs.
        any_arch_re = "E: ELF file .* found in an 'any' package."
        if any(re.search(any_arch_re, line) for line in namcap):
            pkgbuild_contents = pkgbuild_contents.replace(
                "arch=(any)", "arch=({})".format(THIS_ARCH))
        # Repackage.
        (cwd / "PKGBUILD").write_text(pkgbuild_contents)
        _run_shell("makepkg --force --repackage --nodeps", cwd=cwd)
        fullname = _get_fullname()  # The arch may have changed.
        # Python dependencies always get misanalyzed so we just filter them
        # away.  Extension modules unconditionally link to `libpthread` (see
        # output of `python-config --libs`) so filter that away too.  It would
        # be preferable to use a `namcap` option instead, though.
        _run_shell(
            "namcap {} "
            "| grep -v \"W: "
                r"\(Dependency included and not needed"
                r"\|Unused shared library '/usr/lib/libpthread\.so\.0'\)"
            "\" "
            "|| true".
            format(fullname.name),
            cwd=cwd)
        _run_shell("namcap PKGBUILD", cwd=cwd)
        _run_shell("makepkg --printsrcinfo >.SRCINFO", cwd=cwd)
        type(self).build_cache.append(fullname)


class Package(_BasePackage):
    def __init__(self, ref, config, prefer):
        super().__init__()

        self._ref = ref

        stream = StringIO()
        self._srctree = None

        LOGGER.info("Packaging %s %s",
                    self.pkgname, ref.info["info"]["version"])
        self._urls = self._filter_and_sort_urls(ref.info["urls"], prefer)
        if not self._urls:
            raise PackagingError(
                "No URL available for package {!r}.".format(self.pkgname))

        self._find_arch_makedepends()
        for nonpy_dep in [ref for ref in self._makedepends
                          if isinstance(ref, NonPyPackageRef)]:
            _run_shell("if ! pacman -Q {0} >/dev/null 2>&1; then "
                       "sudo pacman -S --asdeps {0}; fi"
                       .format(nonpy_dep.pkgname), verbose=True)
        metadata = _get_metadata(
            ref.orig_name,
            any(ref.pypi_name == "Cython" for ref in self._makedepends
                if isinstance(ref, PackageRef)))
        self._depends = self._find_depends(metadata)
        self._licenses = self._find_license()

        stream.write(PKGBUILD_HEADER.format(pkg=self, config=config))
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
                if wheel_info.py not in PY_TAGS:
                    continue
                try:
                    order = prefer.index(
                        {"any": "anywheel",
                         "manylinux1_i686": "manylinuxwheel",
                         "manylinux1_x86_64": "manylinuxwheel"}[
                             wheel_info.platform])
                except (KeyError, ValueError):
                    continue
                else:
                    # PyPI currently allows uploading of packages with local
                    # version identifiers, see pypa/pypi-legacy#486.
                    if (wheel_info.name != self._ref.wheel_name
                            or wheel_info.version
                               != self._ref.info["info"]["version"]):
                        LOGGER.warning("Unexpected wheel info: %s", wheel_info)
                    else:
                        urls.append((url, order))
            elif url["packagetype"] == "sdist":
                with suppress(ValueError):
                    urls.append((url, prefer.index("sdist")))
            else:  # Skip other dists.
                continue
        return [url for url, key in sorted(urls, key=lambda kv: kv[1])]

    def _get_srctree(self):
        url = next(url for url in self._urls if url["packagetype"] == "sdist")
        if self._srctree is None:
            self._srctree = TemporaryDirectory()
            if urllib.parse.urlparse(url["url"]).scheme.startswith("git+"):
                _subprocess_run(
                    ["git", "clone", url["url"][4:], self._srctree.name])
                self._srctree.path = Path(self._srctree.name)
            else:
                r = urllib.request.urlopen(url["url"])
                tmppath = Path(self._srctree.name, Path(url["path"]).name)
                tmppath.write_bytes(r.read())
                shutil.unpack_archive(str(tmppath), self._srctree.name)
                self._srctree.path = Path(
                    self._srctree.name,
                    "{0._ref.pypi_name}-{0.pkgver}".format(self))
        return self._srctree.path

    def _find_arch_makedepends(self):
        self._arch = ["any"]
        self._makedepends = PackageRefList([PackageRef("pip")])
        archs = sorted(
            {PLATFORM_TAGS[parse_wheel(url["path"]).platform]
             for url in self._urls if url["packagetype"] == "bdist_wheel"})
        if self._urls[0]["packagetype"] == "bdist_wheel":
            self._arch = archs
        else:
            if list(self._get_srctree().glob("**/*.i")):
                self._arch = ["i686", "x86_64"]
                self._makedepends.append(NonPyPackageRef("swig"))
            if list(self._get_srctree().glob("**/*.pyx")):
                self._arch = ["i686", "x86_64"]
                self._makedepends.append(PackageRef("Cython"))
            if not "any" in archs and list(self._get_srctree().glob("**/*.c")):
                # Don't bother checking for the presence of C sources if
                # there's an "any" wheel available; e.g. pexpect has a C source
                # in its *tests*.
                self._arch = ["i686", "x86_64"]

    def _find_depends(self, metadata):
        return PackageRefList(map(PackageRef, metadata["requires"]))

    def _find_license(self):
        info = self._ref.info["info"]
        licenses = []
        license_classes = [
            classifier for classifier in info["classifiers"]
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
        elif info["license"] not in [None, "UNKNOWN"]:
            licenses.append("custom:{}".format(info["license"]))
        else:
            LOGGER.warning("No license information available")
            licenses.append("custom:unknown")

        _license_found = False
        if any(license not in TROVE_COMMON_LICENSES for license in licenses):
            for url in [info["download_url"], info["home_page"]]:
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
                    if path.is_file():
                        self._files.update(LICENSE=path.read_bytes())
                        break
                else:
                    self._files.update(
                        LICENSE=("LICENSE: " + ", ".join(licenses) + "\n")
                                .encode("ascii"))
                    LOGGER.warning("Could not retrieve license file")

        return licenses

    pkgname = property(
        lambda self: self._ref.pkgname)
    epoch = property(
        lambda self:
        self._ref.arch_version.epoch if self._ref.arch_version else "")
    pkgver = property(
        lambda self: shlex.quote(self._ref.info["info"]["version"]))
    pkgrel = property(
        lambda self: "00")
    pkgdesc = property(
        lambda self: shlex.quote(self._ref.info["info"]["summary"]))
    arch = property(
        lambda self: " ".join(self._arch))
    url = property(
        lambda self: shlex.quote(
            next(url for url in [self._ref.info["info"]["home_page"],
                                 self._ref.info["info"]["download_url"],
                                 self._ref.info["info"]["package_url"]]
                 if url not in [None, "UNKNOWN"])))
    license = property(
        lambda self: " ".join(map(shlex.quote, self._licenses)))
    depends = property(
        lambda self: self._depends)
    makedepends = property(
        lambda self: self._makedepends)
    checkdepends = property(
        lambda self: PackageRefList())

    def write_deps_to(self, base_path, *, force, prefer, makepkg):
        for ref in self._depends:
            if not ref.exists:
                # Dependency not found, build it too.
                create_package(ref.pypi_name, force=force, prefer=prefer,
                               makepkg=makepkg, base_path=base_path)


class MultiPackage(_BasePackage):
    def __init__(self, ref, config, prefer):
        super().__init__()
        self._ref = ref
        self._arch_depends = PackageRefList(
            Package(PackageRef(name, force_new=True), config, prefer)
            for name in ref.arch_packaged)
        self._arch_version = self._ref.arch_version._replace(
            pkgrel=self._ref.arch_version.pkgrel + ".99")
        for pkg in self._arch_depends:
            pkg._pkgbuild = pkg._pkgbuild.replace(
                "conflicts=()",
                "conflicts=('{0}<{1}' '{0}>{1}')".format(
                    ref.pkgname, self._arch_version),
                1)
        self._pkgbuild = (
            PKGBUILD_HEADER.format(pkg=self, config=config) +
            MULTIPKGBUILD_CONTENTS)

    pkgname = property(
        lambda self: self._ref.pkgname)
    epoch = property(
        lambda self: self._arch_version.epoch)
    pkgver = property(
        lambda self: self._arch_version.pkgver)
    pkgrel = property(
        lambda self: self._arch_version.pkgrel)
    pkgdesc = property(
        lambda self: "'A wrapper package.'")
    arch = property(
        lambda self: "any")
    url = property(
        lambda self: "N/A")
    license = property(
        lambda self: "CCPL:by")  # Individual components retain their license.
    depends = property(
        lambda self: self._arch_depends)
    makedepends = property(
        lambda self: PackageRefList())
    checkdepends = property(
        lambda self: PackageRefList())

    def _get_target_path(self, base_path):
        return base_path / ("meta:" + self._ref.pkgname)

    def write_deps_to(self, base_path, *, force, prefer, makepkg):
        target_path = self._get_target_path(base_path)
        for pkg in self._arch_depends:
            pkg.write_deps_to(
                target_path, force=force, prefer=prefer, makepkg=makepkg)
            pkg.write_to(target_path, force=force, makepkg=makepkg)

    def write_to(self, base_path, *, force, makepkg):
        target_path = self._get_target_path(base_path)
        super().write_to(target_path, force=force, makepkg=makepkg)


def dispatch_package_builder(name, config, prefer):
    ref = PackageRef(name)
    cls = Package if len(ref.arch_packaged) <= 1 else MultiPackage
    return cls(ref, config, prefer)


@lru_cache()
def get_config():
    with TemporaryDirectory() as tmpdir:
        mini_pkgbuild = ('pkgver=0\npkgrel=0\narch=(any)\n'
                         'prepare() { echo "$PACKAGER"; exit 0; }')
        Path(tmpdir, "PKGBUILD").write_text(mini_pkgbuild)
        maintainer = (
            _run_shell("makepkg", cwd=tmpdir, stdout=PIPE, stderr=PIPE).
            stdout[:-1])  # Strip newline.
    return {"maintainer": maintainer}


@lru_cache()
def create_package(
        name,
        force=False,
        prefer=False,
        skipdeps=False,
        makepkg="--cleanbuild --nodeps",
        base_path=None):

    pkg = dispatch_package_builder(name, get_config(), prefer=prefer)
    if base_path is None:
        base_path = Path()
    if not skipdeps:
        pkg.write_deps_to(
            base_path, force=force, prefer=prefer, makepkg=makepkg)
    pkg.write_to(base_path, force=force, makepkg=makepkg)


def find_outdated():
    syswide_location = (
        "{0.prefix}/lib/python{0.version_info.major}.{0.version_info.minor}"
        "/site-packages".format(sys))
    # `pip show` is rather slow, so just call it once.
    lines = _run_shell("pip list --outdated", stdout=PIPE).stdout.splitlines()
    names = [line.split()[0] for line in lines]
    locs = _run_shell("pip show {} | grep -Po '(?<=^Location: ).*'".
                      format(" ".join(names)), stdout=PIPE).stdout.splitlines()
    owners = {}
    for line, name, loc in zip(lines, names, locs):
        if loc == syswide_location:
            pkgname, pkgver_full = _run_shell(
                # {dist,egg}-info; don't be confused e.g. by pytest-cov.pth.
                # This implementation raises if there's any ambibuity.
                "pacman -Qo {}/{}-*-info | rev | cut -d' ' -f1,2 | rev"
                .format(syswide_location, name.replace("-", "_")),
                stdout=PIPE).stdout[:-1].split()
            # Check that pypi's version is indeed newer.  Some packages
            # mis-report their version to pip (e.g., slicerator 0.9.7's Github
            # release).
            # FIXME(?) Emit a warning?  How does this behave on metapackages?
            pkgver, pkgrel = pkgver_full.split("-")
            *_, pypi_ver, pypi_type = line.split()
            if pkgver == pypi_ver:
                continue
            owners.setdefault("{} {}".format(pkgname, pkgver_full),
                              []).append(line)
    owners = OrderedDict(sorted(owners.items()))
    for owner, lines in owners.items():
        print(owner)
        for line in lines:
            print("\t" + line)
    return owners


_description = """\
Create a PKGBUILD for a PyPI package and run makepkg.

Default arguments can be set in the PYPI2PKGBUILD_ARGS environment variable.
"""


def main():
    try:
        _run_shell("pkgfile pkgfile", stdout=DEVNULL)
    except CalledProcessError:
        # Display one of:
        #   - "/bin/sh: pkgfile: command not found"
        #   - "error: No repo files found. Please run `pkgfile --update'."
        # on stderr.
        sys.exit(1)

    parser = ArgumentParser(
        description=_description,
        formatter_class=type("", (RawDescriptionHelpFormatter,
                                  ArgumentDefaultsHelpFormatter), {}))
    parser.add_argument(
        "name", nargs="?",
        help="The PyPI package name.")
    parser.add_argument(
        "-d", "--debug", action="store_true", default=False,
        help="Log at DEBUG level.")
    parser.add_argument(
        "-o", "--outdated", action="store_true", default=False,
        help="Find outdated packages.")
    parser.add_argument(
        "-u", "--update-outdated", metavar="SKIP", nargs="*",
        help="Find and build outdated packages; pass a list of (exact) PyPI"
             "names to *skip*.")
    parser.add_argument(
        "-b", "--base-path", type=Path,
        help="Base path where the packages folders are created.")
    parser.add_argument(
        "-f", "--force", action="store_true",
        help="Overwrite a previously existing PKGBUILD.")
    parser.add_argument(
        "-p", "--prefer", metavar="P",
        default="anywheel:sdist:manylinuxwheel",
        type=lambda s: tuple(s.split(":")),  # Keep it hashable.
        help="Preference order for dists.")
    parser.add_argument(
        "-s", "--skipdeps", action="store_true",
        help="Don't generate PKGBUILD for dependencies.")
    parser.add_argument(
        "-m", "--makepkg", metavar="M",
        default="--cleanbuild --nodeps",
        help="Additional arguments to pass to makepkg.")
    env_args = eval(  # Parse the environment variable arguments.
        _run_shell(
            "{} -c 'import sys; print(sys.argv[1:])' {}".format(
                sys.executable, os.environ.get("PYPI2PKGBUILD_ARGS", "")),
            stdout=PIPE).stdout)
    args = parser.parse_args(env_args + sys.argv[1:])
    logging.basicConfig(level="DEBUG" if vars(args).pop("debug") else "INFO")

    outdated, update_outdated = (
        vars(args).pop(k) for k in ["outdated", "update_outdated"])
    if outdated or update_outdated is not None:
        if args.name:
            parser.error("--outdated{,-update} should be given with no name.")
        owners = find_outdated()
        if update_outdated is not None:
            for line in sum(owners.values(), []):
                name, *_ = line.split()
                if name in update_outdated:
                    continue
                create_package(**{**vars(args), "name": name})
        else:
            return

    else:
        if not args.name:
            parser.error("the following arguments are required: name")
        try:
            create_package(**vars(args))
        except PackagingError as e:
            print(e, file=sys.stderr)
            return 1

    if Package.build_cache:
        cmd = "sudo pacman -U {} {}".format(
            "-dd" if args.skipdeps else "",
            " ".join(map(str, Package.build_cache)))
        _run_shell(cmd, check=False, verbose=True)


if __name__ == "__main__":
    sys.exit(main())
