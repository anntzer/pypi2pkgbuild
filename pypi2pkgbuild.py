#!/usr/bin/env python
"""Convert PyPI entries to Arch Linux packages.
"""

import abc
from abc import ABC
from argparse import (Action, ArgumentParser, ArgumentDefaultsHelpFormatter,
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
from subprocess import CalledProcessError, PIPE
import sys
from tempfile import NamedTemporaryFile, TemporaryDirectory
import textwrap
import urllib.request

from pip._vendor.distlib.util import normalize_name as distlib_normalize_name
from pip._vendor.packaging.version import parse as version_parse
from pip.vcs import VersionControl

try:
    import _pypi2pkgbuild_version
except ImportError:
    from pip._vendor import pkg_resources
    __version__ = pkg_resources.get_distribution("pypi2pkgbuild").version
else:
    __version__ = _pypi2pkgbuild_version.get_versions()["version"]


LOGGER = logging.getLogger(Path(__file__).stem)

PY_TAGS = ["py2.py3",
           "py{0.major}".format(sys.version_info),
           "cp{0.major}{0.minor}".format(sys.version_info)]
PLATFORM_TAGS = {
    "any": "any", "manylinux1_i686": "i686", "manylinux1_x86_64": "x86_64"}
THIS_ARCH = ["i686", "x86_64"][sys.maxsize > 2 ** 32]
SDIST_SUFFIXES = [".tar.gz", ".tgz", ".tar.bz2", ".zip"]
LICENSE_NAMES = ["LICENSE", "LICENSE.txt", "license.txt",
                 "COPYING.md", "COPYING.rst", "COPYING.txt",
                 "COPYRIGHT"]
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
depends=(python {pkg.depends:{pkg.__class__.__name__}})
## EXTRA_DEPENDS ##
makedepends=({pkg.makedepends:{pkg.__class__.__name__}})
checkdepends=({pkg.checkdepends:{pkg.__class__.__name__}})
provides=({pkg.provides})
conflicts=(${{provides%=*}})  # No quotes, to avoid an empty entry.
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
if [[ ${source[0]} =~ ^git+ ]]; then
    provides+=("${pkgname%-git}")
    conflicts+=("${pkgname%-git}")
fi

export PIP_CONFIG_FILE=/dev/null
export PIP_DISABLE_PIP_VERSION_CHECK=true

_first_source() {
    echo " ${source_i686[@]} ${source_x86_64[@]} ${source[@]}" |
        tr -s ' ' | tail -c+2 | cut -d' ' -f1
}

_is_wheel() {
    [[ $(_first_source) =~ \\.whl$ ]]
}

_dist_name() {
    basename "$(_first_source)" |
      sed 's/\\(""" + re.escape("|".join(SDIST_SUFFIXES)) + """\\|\\.git\\)$//'
}

if [[ $(_first_source) =~ ^git+ ]]; then
    pkgver() {
        ( set -o pipefail
          cd "$srcdir/$(_dist_name)"
          git describe --long --tags 2>/dev/null |
            sed 's/^v//;s/\\([^-]*-g\\)/r\\1/;s/-/./g' ||
          printf "r%s.%s" \\
              "$(git rev-list --count HEAD)" "$(git rev-parse --short HEAD)"
        )
    }
fi

build() {
    if _is_wheel; then return; fi
    cd "$srcdir/$(_dist_name)"
    # See Arch Wiki/PKGBUILD/license.
    # Get the first filename that matches.
    local test_name
    if [[ ${license[0]} =~ ^(BSD|MIT|ZLIB|Python)$ ]]; then
        for test_name in """ + " ".join(LICENSE_NAMES) + """; do
            if cp "$srcdir/$(_dist_name)/$test_name" "$srcdir/LICENSE" 2>/dev/null; then
                break
            fi
        done
    fi
    # Build the wheel (which can fail) only after fetching the license.
    pip wheel -v --no-deps --wheel-dir="$srcdir" \\
        --global-option=build --global-option=-j"$(nproc)" . ||
        true
}

check() {
    # Remove the first line line to run tests.
    # You may need to call `python setup.py build_ext -i` first.
    return 0
    if _is_wheel; then return; fi
    cd "$srcdir/$(_dist_name)"
    python setup.py -q test
}

package() {
    cd "$srcdir"
    # pypa/pip#3063: pip always checks for a globally installed version.
    pip --quiet install --root="$pkgdir" --no-deps --ignore-installed \\
        "$(ls ./*.whl 2>/dev/null || echo ./"$(_dist_name)")"
    if [[ -f LICENSE ]]; then
        install -D -m644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
    fi
}

. "$(dirname "$BASH_SOURCE")/PKGBUILD_EXTRAS"
"""

METAPKGBUILD_CONTENTS = """\
package() {
    true
}
"""


def _unique(seq):
    """Return unique elements in a sequence, keeping them in order.
    """
    return list(OrderedDict(zip(list(seq)[::-1], repeat(None))))[::-1]


def _run_shell(args, **kwargs):
    """Logging wrapper for `subprocess.run`, with useful defaults.

    Log at `DEBUG` level except if the `verbose` kwarg is set, in which case
    log at `INFO` level.
    """
    kwargs = {"shell": isinstance(args, str),
              "env": {**os.environ,
                      "LC_ALL": "C",  # So that text outputs can be parsed.
                      "PYTHONNOUSERSITE": "1",
                      "PIP_CONFIG_FILE": "/dev/null"},
              "check": True,
              "universal_newlines": True,
              **kwargs}
    if "cwd" in kwargs:
        kwargs["cwd"] = str(Path(kwargs["cwd"]))
    level = logging.INFO if kwargs.pop("verbose", None) else logging.DEBUG
    args_s = (args if isinstance(args, str)
              else " ".join(map(shlex.quote, args)))
    if "cwd" in kwargs:
        LOGGER.log(level,
                   "Running subprocess from %s:\n%s", kwargs["cwd"], args_s)
    else:
        LOGGER.log(level, "Running subprocess:\n%s", args_s)
    return subprocess.run(args, **kwargs)


class ArchVersion(namedtuple("_ArchVersion", "epoch pkgver pkgrel")):
    @classmethod
    def parse(cls, s):
        epoch, pkgver, pkgrel = (
            re.fullmatch(r"(?:(.*):)?(.*)-(.*)", s).groups())
        return cls(epoch or "", pkgver, pkgrel)

    def __str__(self):
        return ("{0.epoch}:{0.pkgver}-{0.pkgrel}" if self.epoch
                else "{0.pkgver}-{0.pkgrel}").format(self)


class WheelInfo(
        namedtuple("_WheelInfo", "name version build python abi platform")):
    @classmethod
    def parse(cls, fname):
        parts = Path(fname).stem.split("-")
        if len(parts) == 5:
            name, version, python, abi, platform = parts
            build = ""
        elif len(parts) == 6:
            name, version, build, python, abi, platform = parts
        else:
            raise ValueError("Invalid wheel name: {}".format(fname))
        return cls(name, version, build, python, abi, platform)


def to_wheel_name(pep503_name):
    return pep503_name.replace("-", "_")


class PackagingError(Exception):
    pass


@lru_cache()
def _get_url_impl(url):
    cache_dir = TemporaryDirectory()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.startswith("git+"):
        _run_shell(["git", "clone", "--recursive", url[4:]],
                    cwd=cache_dir.name)
    elif parsed.scheme == "pip":
        try:
            _run_shell(["pip", "download", "--no-deps", "-d", cache_dir.name,
                        *(parsed.fragment.split() if parsed.fragment else []),
                        parsed.netloc])
        except CalledProcessError:
            # pypa/pip#1884: download can "fail" due to buggy setup.py (e.g.
            # astropy 1.3.3).
            raise PackagingError("Failed to download {}, possibly due to a "
                                 "buggy setup.py".format(parsed.netloc))
    else:
        Path(cache_dir.name, Path(parsed.path).name).write_bytes(
            urllib.request.urlopen(url).read())
    packed_path, = (path for path in Path(cache_dir.name).iterdir())
    # Keep a reference to the TemporaryDirectory.
    return cache_dir, packed_path


def _get_url_packed_path(url):
    cache_dir, packed_path = _get_url_impl(url)
    return packed_path


@lru_cache()
def _get_url_unpacked_path_or_null(url):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "file" and parsed.path.endswith(".whl"):
        return Path("/dev/null")
    try:
        cache_dir, packed_path = _get_url_impl(url)
    except CalledProcessError:
        return Path("/dev/null")
    if packed_path.is_file():  # pip://
        shutil.unpack_archive(str(packed_path), cache_dir.name)
    unpacked_path, = (
        path for path in Path(cache_dir.name).iterdir() if path.is_dir())
    return unpacked_path


@lru_cache()
def _guess_url_makedepends(url, guess_makedepends):
    parsed = urllib.parse.urlparse(url)
    makedepends = [PackageRef("pip")]
    if ("swig" in guess_makedepends
            and list(_get_url_unpacked_path_or_null(url).glob("**/*.i"))):
        makedepends.append(NonPyPackageRef("swig"))
    if ("cython" in guess_makedepends
            and list(_get_url_unpacked_path_or_null(url).glob("**/*.pyx"))):
        makedepends.append(PackageRef("Cython"))
    return DependsTuple(makedepends)


@lru_cache()
def _get_metadata(name, setup_requires):
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
            NamedTemporaryFile("r") as more_requires_log, \
            NamedTemporaryFile("r") as log:
        script = textwrap.dedent(r"""
        set -e
        python -mvenv {venvdir}
        # Don't stay in the source folder, which may contain wheels/sdists/etc.
        cd {venvdir}
        . '{venvdir}/bin/activate'
        pip install --upgrade {setup_requires} >/dev/null
        install_cmd() {{
            pip freeze | cut -d= -f1 >'{venvdir}/pre_install_list'
            if ! pip install --no-deps '{req}'; then
                return 1
            fi
            pip freeze | cut -d= -f1 >'{venvdir}/post_install_list'
            # installed name, or real name if it doesn't appear (setuptools,
            # pip, Cython, numpy).
            install_name="$(comm -13 '{venvdir}/pre_install_list' \
                                     '{venvdir}/post_install_list')"
            # the requirement can be 'req_name==version', or a path name.
            if [[ -z "$install_name" ]]; then
                if [[ -e '{req}' ]]; then
                    install_name="$(basename '{req}' .git)"
                else
                    install_name="$(echo '{req}' | cut -d= -f1 -)"
                fi
            fi
        }}
        show_cmd() {{
            python <<EOF
        import json, pip
        info = next(pip.commands.show.search_packages_info(['$install_name']))
        info.pop('entry_points', None)
        print(json.dumps(info))
        EOF
        }}
        if install_cmd >{log.name}; then
            show_cmd
        else
            pip install numpy >/dev/null
            echo numpy >>{more_requires_log.name}
            install_cmd >{log.name}
            show_cmd
        fi
        """).format(
            venvdir=venvdir,
            setup_requires=" ".join(setup_requires),
            req=(_get_url_unpacked_path_or_null(name)
                 if name.startswith("git+") else name),
            more_requires_log=more_requires_log,
            log=log)
        try:
            process = _run_shell(script, stdout=PIPE)
        except CalledProcessError:
            sys.stderr.write(log.read())
            raise PackagingError(
                "Failed to obtain metadata for {}.".format(name))
        more_requires = more_requires_log.read().splitlines()
    metadata = json.loads(process.stdout)
    metadata["requires"].extend(more_requires)
    return {key.replace("-", "_"): value for key, value in metadata.items()}


@lru_cache()
def _get_info(name, *,
              pre=False,
              guess_makedepends=(),
              _sources=("git", "local", "pypi"),
              _version=""):

    parsed = urllib.parse.urlparse(name)

    def _get_info_git():
        if not parsed.scheme.startswith("git+"):
            return
        url, rev = VersionControl(name).get_url_rev()
        if rev:
            # FIXME pip guesses whether a name is a branch, a commit or a tag,
            # whereas the fragment type must be specified in the PKGBUILD.
            # FIXME fragment support.
            raise PackagingError(
                "No support for packaging specific revisions.")
        metadata = _get_metadata(
            name, _guess_url_makedepends(name, guess_makedepends).pep503_names)
        try:  # Normalize the name if available on PyPI.
            metadata["name"] = _get_info(
                metadata["name"], _sources=("pypi",))["info"]["name"]
        except PackagingError:
            pass
        return {"info": {"download_url": url,
                         "home_page": url,
                         "package_url": url,
                         **metadata},
                "urls": [{"packagetype": "sdist",
                          "path": parsed.path,
                          "url": name,
                          "md5_digest": "SKIP"}]}

    def _get_info_local():
        if not parsed.scheme == "file":
            return
        metadata = _get_metadata(
            name, _guess_url_makedepends(name, guess_makedepends).pep503_names)
        return {"info": {"download_url": name,
                         "home_page": name,
                         "package_url": name,
                         **metadata},
                "urls": [{"packagetype":
                              "bdist_wheel" if parsed.path.endswith(".whl")
                              else "sdist",
                          "path": parsed.path,
                          "url": name,
                          "md5_digest": "SKIP"}]}

    def _get_info_pypi():
        try:
            r = urllib.request.urlopen(
                "https://pypi.python.org/pypi/{}/{}/json"
                .format(name, _version))
        except urllib.error.HTTPError:
            return
        # Load as OrderedDict so that always the same sdist is chosen if e.g.
        # both zip and tgz are available.
        request = json.loads(r.read().decode(r.headers.get_param("charset")),
                             object_pairs_hook=OrderedDict)
        if not _version:
            versions = [
                version for version in
                (version_parse(release) for release in request["releases"])
                if not (not pre and version.is_prerelease)]
            if not versions:
                raise PackagingError(
                    "No suitable release found."
                    if not request["releases"] else
                    "No suitable release found.  Pre-releases are available, "
                    "use --pre to use the latest one.")
            max_version = str(max(version for version in versions))
            if max_version != request["info"]["version"]:
                return _get_info(name, pre=pre, _version=max_version)
        return request

    for source in _sources:
        info = locals()["_get_info_{}".format(source)]()
        if info:
            return info
    else:
        raise PackagingError("Package {} not found.".format(
            " ".join(filter(None, [name, _version]))))


def _get_site_packages_location():
    return (
        "{0.prefix}/lib/python{0.version_info.major}.{0.version_info.minor}"
        "/site-packages".format(sys))


# For _find_{installed,arch}_name_version:
#   - first check for a matching `.{dist,egg}-info` file, ignoring case to
#     handle e.g. `cycler` (pip) / `Cycler` (PyPI).
#   - then check exact lowercase matches, to handle packages without a
#     `.{dist,egg}-info`.


def _find_installed_name_version(pep503_name, *, ignore_vendored=False):
    parts = (
        _run_shell(
            "(shopt -s nocaseglob; pacman -Qo {}/{}-*-info 2>/dev/null) "
            "| rev | cut -d' ' -f1,2 | rev".format(
                _get_site_packages_location(), to_wheel_name(pep503_name)),
            stdout=PIPE).stdout[:-1].split()
        or _run_shell(
            "pacman -Q python-{} 2>/dev/null".format(pep503_name),
            stdout=PIPE, check=False).stdout[:-1].split())
    if parts:
        pkgname, version = parts  # This will raise if there is an ambiguity.
        if pkgname.endswith("-git"):
            expected_conflict = pkgname[:-len("-git")]
            if _run_shell(
                    "pacman -Qi {} 2>/dev/null "
                    "| grep -q 'Conflicts With *: {}$'"
                    .format(pkgname, expected_conflict),
                    check=False).returncode == 0:
                pkgname = pkgname[:-len("-git")]
            else:
                raise PackagingError(
                    "Found installed package {} which does NOT conflict with "
                    "{}.  Please uninstall it first."
                    .format(pkgname, expected_conflict))
        if ignore_vendored and pkgname.startswith("python--"):
            return
        else:
            return pkgname, ArchVersion.parse(version)
    else:
        return


def _find_arch_name_version(pep503_name):
    for standalone in [True, False]:  # vendored into another Python package?
        *candidates, = map(str.strip, _run_shell(
            "pkgfile -riv "
            "'^/usr/lib/python{version.major}\.{version.minor}/{parent}"
            r"{wheel_name}-.*py{version.major}\.{version.minor}\.egg-info' "
            "| cut -f1 | uniq | cut -d/ -f2".format(
                parent="site-packages/" if standalone else "",
                wheel_name=to_wheel_name(pep503_name),
                version=sys.version_info),
            stdout=PIPE).stdout[:-1].splitlines())
        if len(candidates) > 1:
            raise PackagingError(
                "Multiple candidates for {}: {}.".format(
                    pep503_name, ", ".join(candidates)))
        elif len(candidates) == 1:
            pkgname, version = candidates[0].split()
            arch_version = ArchVersion.parse(version)
            return pkgname, arch_version


class NonPyPackageRef:
    def __init__(self, pkgname):
        self.pkgname = self.depname = pkgname


class PackageRef:
    def __init__(self, name, *,
                 pre=False, guess_makedepends=(), subpkg_of=None):
        # If `subpkg_of` is set, do not attempt to use the Arch Linux name,
        # and name the package python--$pkgname to prevent collision.
        self.orig_name = name  # A name or an URL.
        self.info = _get_info(
            name, pre=pre, guess_makedepends=guess_makedepends)
        self.pypi_name = self.info["info"]["name"]
        # pacman -Slq | grep '^python-' | cut -d- -f 2- |
        #     grep -v '^\([[:alnum:]]\)*$' | grep '_'
        # (or '\.', or '-') shows that PEP503 normalization is by far the most
        # common, so we use it everywhere... except when downloading, which
        # requires the actual PyPI-registered name.
        self.pep503_name = distlib_normalize_name(self.pypi_name)

        if subpkg_of:
            pkgname = "python--{}".format(self.pep503_name)
            depname = subpkg_of.pkgname
            arch_version = None

        else:
            # For the name as package:  First, check installed packages,
            # which may have inherited non-standard names from the AUR (e.g.,
            # `python-numpy-openblas`, `pipdeptree`).  Specifically ignore
            # vendored packages (`python--*`).  Then, check official packages.
            # Then, fallback on the default.
            # For the name as dependency, try the official name first, so that
            # one can replace the local package (which provides the official
            # one anyways) by the official one if desired without breaking
            # dependencies.

            installed = _find_installed_name_version(
                self.pep503_name, ignore_vendored=True)
            arch = _find_arch_name_version(self.pep503_name)
            default = "python-{}".format(self.pep503_name), None
            pkgname, arch_version = installed or arch or default
            depname, _ = arch or installed or default

        arch_packaged = _run_shell(
            "pkgfile -l {} 2>/dev/null"
            # Package name has no dash (per packaging standard) nor slashes
            # (which can occur when a subpackage is vendored (depending on how
            # it is done), e.g. `.../foo.egg-info` and `.../foo/bar.egg-info`
            # both existing).
            r"| grep -Po '(?<=site-packages/)[^-/]*(?=.*\.egg-info/?$)'".
            format(pkgname), stdout=PIPE, check=False).stdout.splitlines()

        # Final values.
        self.pkgname = (
            "{}-git" if name.startswith("git+") else "{}").format(pkgname)
        # Packages that depend on a vendored package should list the
        # metapackage (which may be otherwise unrelated) as a dependency, so
        # that the metapackage can get updated into an official package without
        # breaking dependencies.
        # However, the owning metapackage should list their vendorees
        # explicitly, so that they do not end up unrequired (other metapackages
        # don't matter as they only depend on their own components).
        # This logic is implemented in `DependsTuple.__fmt__`.
        self.depname = depname
        self.arch_version = arch_version
        self.arch_packaged = arch_packaged
        self.exists = arch_version is not None


class DependsTuple(tuple):  # Keep it hashable.
    @property
    def pep503_names(self):
        # Needs to be hashable.
        return tuple(ref.pep503_name for ref in self
                     if isinstance(ref, PackageRef))

    def __format__(self, fmt):
        # See above re: dependency type.
        if fmt == "Package":
            return " ".join(_unique(ref.depname for ref in self))
        elif fmt == "MetaPackage":
            return " ".join(_unique(ref.pkgname for ref in self))
        else:
            return super().__format__(fmt)  # Raise TypeError.


class _BasePackage(ABC):
    build_cache = OrderedDict() # package_name: (package path, is_dep)

    def __init__(self):
        self._files = OrderedDict()
        # self._pkgbuild = ...

    @abc.abstractmethod
    def write_deps_to(self, options):
        pass

    def write_to(self, options):
        cwd = options.base_path / self.pkgname
        cwd.mkdir(parents=True, exist_ok=options.force)
        (cwd / "PKGBUILD_EXTRAS").write_text(options.pkgbuild_extras)
        (cwd / "PKGBUILD").write_text(self._pkgbuild)
        for fname, content in self._files.items():
            (cwd / fname).write_bytes(content)
        if isinstance(self, Package):
            srctree = _get_url_packed_path(self._get_pip_url())
            dest = cwd / srctree.name
            with suppress(FileNotFoundError):
                if dest.is_dir():
                    shutil.rmtree(dest)
                else:
                    dest.unlink()
            shutil.move(srctree, dest)
        cmd = ["makepkg",
               *(["--force"] if options.force else []),
               *shlex.split(options.makepkg)]
        _run_shell(cmd, cwd=cwd)

        def _get_fullpath():
            # Only one of the archs will be globbed successfully.
            fullpath, = sum(
                (list(cwd.glob(fname + ".*"))
                 for fname in (
                     _run_shell("makepkg --packagelist", cwd=cwd, stdout=PIPE)
                     .stdout.splitlines())),
                [])
            return fullpath

        fullpath = _get_fullpath()
        # Update PKGBUILD.
        needs_rebuild = False
        namcap = (_run_shell(["namcap", fullpath.name], cwd=cwd, stdout=PIPE)
                  .stdout.splitlines())
        # `pkgver()` may update the PKGBUILD, so reread it.
        pkgbuild_contents = (cwd / "PKGBUILD").read_text()
        # Binary dependencies.
        extra_deps_re = "(?<=E: Dependency ).*(?= detected and not included)"
        extra_deps = [
            match.group(0)
            for match in map(re.compile(extra_deps_re).search, namcap)
            if match]
        pkgbuild_contents = pkgbuild_contents.replace(
            "## EXTRA_DEPENDS ##",
            "depends+=({})".format(" ".join(extra_deps)))
        if extra_deps:
            needs_rebuild = True
        # Unexpected arch-dependent package (e.g. direct compilation of C
        # source).
        any_arch_re = "E: ELF file .* found in an 'any' package."
        if any(re.search(any_arch_re, line) for line in namcap):
            pkgbuild_contents = re.sub(
                "(?m)^arch=.*$",
                "arch=({})".format(THIS_ARCH),
                pkgbuild_contents,
                1)
            needs_rebuild = True
        if needs_rebuild:
            # Remove previous package, repackage, and get new name (arch may
            # have changed).
            fullpath.unlink()
            (cwd / "PKGBUILD").write_text(pkgbuild_contents)
            _run_shell("makepkg --force --repackage --nodeps", cwd=cwd)
            fullpath = _get_fullpath()
        # Python dependencies always get misanalyzed so we just filter them
        # away.  Extension modules unconditionally link to `libpthread` (see
        # output of `python-config --libs`) so filter that away too.  It would
        # be preferable to use a `namcap` option instead, though.
        _run_shell(
            "namcap {} "
            "| grep -v \"W: "
                r"\(Dependency included and not needed"
                r"\|Unused shared library '/usr/lib/libpthread\.so\.0'\)"
            "\" || "
            "true".format(fullpath.name),
            cwd=cwd)
        _run_shell("namcap PKGBUILD", cwd=cwd)
        _run_shell("makepkg --printsrcinfo >.SRCINFO", cwd=cwd)
        type(self).build_cache[self.pkgname] = (fullpath, options.is_dep)


class Package(_BasePackage):
    def __init__(self, ref, config, options):
        super().__init__()

        self._ref = ref
        self._pkgrel = options.pkgrel

        stream = StringIO()

        LOGGER.info("Packaging %s %s.",
                    self.pkgname, ref.info["info"]["version"])
        self._urls = self._filter_and_sort_urls(
            ref.info["urls"], options.pkgtypes)
        if not self._urls:
            raise PackagingError(
                "No URL available for package {}.".format(self.pkgname))

        self._find_arch_makedepends(options)
        for nonpy_dep in [ref for ref in self._makedepends
                          if isinstance(ref, NonPyPackageRef)]:
            _run_shell("if ! pacman -Q {0} >/dev/null 2>&1; then "
                       "sudo pacman -S --asdeps {0}; fi"
                       .format(nonpy_dep.pkgname), verbose=True)
        metadata = _get_metadata(
            "{}=={}".format(ref.orig_name, self.pkgver)
            if urllib.parse.urlparse(ref.orig_name).scheme == ""
            else ref.orig_name,
            self._makedepends.pep503_names)
        self._depends = DependsTuple(
            PackageRef(req)
            if options.build_deps else
            # FIXME Could use something slightly better, i.e. still check local
            # packages...
            NonPyPackageRef("python-{}".format(distlib_normalize_name(req)))
            for req in metadata["requires"])
        self._licenses = self._find_license()

        stream.write(PKGBUILD_HEADER.format(pkg=self, config=config))
        if self._urls[0]["packagetype"] == "bdist_wheel":
            # Either just "any", or some specific archs.
            for url in self._urls:
                if url["packagetype"] != "bdist_wheel":
                    continue
                wheel_info = WheelInfo.parse(url["path"])
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

    def _filter_and_sort_urls(self, unfiltered_urls, pkgtypes):
        urls = []
        for url in unfiltered_urls:
            if url["packagetype"] == "bdist_wheel":
                wheel_info = WheelInfo.parse(url["path"])
                if wheel_info.python not in PY_TAGS:
                    continue
                try:
                    order = pkgtypes.index(
                        {"any": "anywheel",
                         "manylinux1_i686": "manylinuxwheel",
                         "manylinux1_x86_64": "manylinuxwheel"}[
                             wheel_info.platform])
                except (KeyError, ValueError):
                    continue
                else:
                    # - The wheel name seems to use the *non-lowercased* but
                    #   otherwise normalized name.  Just lowercase it too for
                    #   simplicity.
                    # - PyPI currently allows uploading of packages with local
                    #   version identifiers, see pypa/pypi-legacy#486.
                    if (wheel_info.name.lower()
                            != to_wheel_name(self._ref.pep503_name)
                        or wheel_info.version
                            != self._ref.info["info"]["version"]):
                        LOGGER.warning("Unexpected wheel info: %s", wheel_info)
                    else:
                        urls.append((url, order))
            elif url["packagetype"] == "sdist":
                with suppress(ValueError):
                    urls.append((url, pkgtypes.index("sdist")))
            else:  # Skip other dists.
                continue
        return [url for url, key in sorted(urls, key=lambda kv: kv[1])]

    def _get_first_package_type(self):
        return self._urls[0]["packagetype"]

    def _get_sdist_url(self):
        parsed = urllib.parse.urlparse(self._ref.orig_name)
        return (self._ref.orig_name
                if re.match(r"\A(git\+|file\Z)", parsed.scheme)
                else "pip://{}=={}#--no-binary=:all:".format(
                    self._ref.pypi_name, self.pkgver))

    def _get_pip_url(self):
        parsed = urllib.parse.urlparse(self._ref.orig_name)
        return (self._ref.orig_name
                if re.match(r"\A(git\+|file\Z)", parsed.scheme)
                else "pip://{}=={}".format(self._ref.pypi_name, self.pkgver))

    def _find_arch_makedepends(self, options):
        if self._get_first_package_type() == "bdist_wheel":
            self._arch = sorted(
                {PLATFORM_TAGS[WheelInfo.parse(url["path"]).platform]
                 for url in self._urls if url["packagetype"] == "bdist_wheel"})
            self._makedepends = DependsTuple(
                map(PackageRef, ["pip", *options.setup_requires]))
        else:
            self._arch = ["any"]
            self._makedepends = DependsTuple((
                *map(PackageRef, options.setup_requires),
                *_guess_url_makedepends(
                    self._get_sdist_url(), options.guess_makedepends)))

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
            LOGGER.warning("No license information available.")
            licenses.append("custom:unknown")

        _license_found = False
        if any(license not in TROVE_COMMON_LICENSES for license in licenses):
            for url in [info["download_url"], info["home_page"]]:
                parsed = urllib.parse.urlparse(url or "")  # Could be None.
                if len(Path(parsed.path).parts) != 3:  # ["/", user, name]
                    continue
                # Strip final slash for later manipulations.
                parsed = parsed._replace(path=re.sub("/$", "", parsed.path))
                if parsed.netloc in ["github.com", "www.github.com"]:
                    parsed = parsed._replace(
                        netloc="raw.githubusercontent.com")
                elif parsed.netloc in ["bitbucket.org", "www.bitbucket.org"]:
                    parsed = parsed._replace(
                        path=parsed.path + "/raw")
                else:
                    continue
                for license_name in LICENSE_NAMES:
                    try:
                        r = urllib.request.urlopen(
                            urllib.parse.urlunparse(
                                parsed._replace(path=parsed.path + "/master/"
                                                     + license_name)))
                    except urllib.error.HTTPError:
                        pass
                    else:
                        self._files.update(LICENSE=r.read())
                        _license_found = True
                        break
                if _license_found:
                    break
            else:
                for path in map(
                        _get_url_unpacked_path_or_null(
                            self._get_sdist_url()).joinpath,
                        LICENSE_NAMES):
                    if path.is_file():
                        self._files.update(LICENSE=path.read_bytes())
                        break
                else:
                    self._files.update(
                        LICENSE=("LICENSE: " + ", ".join(licenses) + "\n")
                                .encode("ascii"))
                    LOGGER.warning("Could not retrieve license file.")

        return licenses

    pkgname = property(
        lambda self: self._ref.pkgname)
    epoch = property(
        lambda self:
        self._ref.arch_version.epoch if self._ref.arch_version else "")
    pkgver = property(
        lambda self: shlex.quote(self._ref.info["info"]["version"]))
    pkgrel = property(
        lambda self: self._pkgrel)
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
        lambda self: DependsTuple())

    @property
    def provides(self):
        # Packages should provide their official alias (e.g. for dependents of
        # `python-numpy-openblas`)... except for vendored packages (so that
        # `python--pillow` doesn't provide `python-pillow`).
        if self._ref.pkgname.startswith("python--"):
            return ""
        try:
            name, version = _find_arch_name_version(self._ref.pep503_name)
        except TypeError:  # name, version = None
            return ""
        else:
            return "{}={}".format(name, self.pkgver)

    def write_deps_to(self, options):
        for ref in self._depends:
            if not ref.exists:
                # Dependency not found, build it too.
                create_package(ref.pep503_name, options._replace(is_dep=True))


class MetaPackage(_BasePackage):
    def __init__(self, ref, config, options):
        super().__init__()
        self._ref = ref
        self._arch_version = self._ref.arch_version._replace(
            pkgrel=self._ref.arch_version.pkgrel + ".99")
        self._subpkgrefs = DependsTuple(
            PackageRef(name, subpkg_of=ref, pre=options.pre)
            for name in ref.arch_packaged)
        self._subpkgs = [
            Package(ref, config, options) for ref in self._subpkgrefs]
        for pkg in self._subpkgs:
            pkg._pkgbuild = re.sub(
                "(?m)^conflicts=.*$",
                "conflicts=('{0}<{1}' '{0}>{1}')".format(
                    ref.pkgname, self._arch_version),
                pkg._pkgbuild,
                1)
        self._pkgbuild = (
            PKGBUILD_HEADER.format(pkg=self, config=config) +
            METAPKGBUILD_CONTENTS)

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
        lambda self: self._subpkgrefs)
    makedepends = property(
        lambda self: DependsTuple())
    checkdepends = property(
        lambda self: DependsTuple())
    provides = property(
        lambda self: "")

    def _get_target_path(self, base_path):
        return base_path / ("meta:" + self._ref.pkgname)

    def write_deps_to(self, options):
        dep_options = options._replace(
            base_path=self._get_target_path(options.base_path),
            is_dep=True)
        for pkg in self._subpkgs:
            pkg.write_deps_to(dep_options)
            pkg.write_to(dep_options)

    def write_to(self, options):
        super().write_to(options._replace(
            base_path=self._get_target_path(options.base_path)))


def dispatch_package_builder(name, config, options):
    ref = PackageRef(
        name, pre=options.pre, guess_makedepends=options.guess_makedepends)
    cls = Package if len(ref.arch_packaged) <= 1 else MetaPackage
    return cls(ref, config, options)


@lru_cache()
def get_config():
    with TemporaryDirectory() as tmpdir:
        mini_pkgbuild = (
            "pkgname=_\n"
            "pkgver=0\n"
            "pkgrel=0\n"
            "arch=(any)\n"
            'prepare() { printf "%s" "$PACKAGER"; exit 0; }')
        Path(tmpdir, "PKGBUILD").write_text(mini_pkgbuild)
        try:
            maintainer = _run_shell(
                "makepkg", cwd=tmpdir, stdout=PIPE, stderr=PIPE).stdout
        except CalledProcessError as e:
            sys.stderr.write(e.stderr)
            raise
    return {"maintainer": maintainer}


@lru_cache()
def create_package(name, options):
    pkg = dispatch_package_builder(name, get_config(), options)
    if options.build_deps:
        pkg.write_deps_to(options)
    pkg.write_to(options)


def find_outdated():
    syswide_location = (
        "{0.prefix}/lib/python{0.version_info.major}.{0.version_info.minor}"
        "/site-packages".format(sys))
    # Skip the `--format` warning (when the default changes, switch to
    # supporting only pip 9 instead).
    lines = (_run_shell("pip list --outdated 2>/dev/null", stdout=PIPE)
             .stdout.splitlines())
    if not lines:
        return {}
    names = [line.split()[0] for line in lines]
    # `pip show` is rather slow, so just call it once.
    locs = _run_shell("pip show {} 2>/dev/null "
                      "| grep -Po '(?<=^Location: ).*'".
                      format(" ".join(names)), stdout=PIPE).stdout.splitlines()
    owners = {}
    for line, name, loc in zip(lines, names, locs):
        if loc == syswide_location:
            pkgname, arch_version = _find_installed_name_version(name)
            # Check that pypi's version is indeed newer.  Some packages
            # mis-report their version to pip (e.g., slicerator 0.9.7's Github
            # release).
            *_, pypi_ver, pypi_type = line.split()
            if arch_version.pkgver == pypi_ver:
                LOGGER.warning(
                    "pip thinks that %s is outdated, but the installed "
                    "version is actually %s, and up-to-date.", name, pypi_ver)
                continue
            owners.setdefault("{} {}".format(pkgname, arch_version),
                              []).append(line)
    owners = OrderedDict(sorted(owners.items()))
    for owner, lines in owners.items():
        print(owner)
        for line in lines:
            print("\t" + line)
    return owners


Options = namedtuple(
    "Options", "base_path force pre pkgrel guess_makedepends setup_requires "
               "pkgtypes build_deps pkgbuild_extras makepkg is_dep")
def main():

    class CommaSeparatedList(Action):
        def __init__(self, *args, **kwargs):
            kwargs["default"] = (() if "default" not in kwargs
                                 else tuple(kwargs["default"]))
            super().__init__(*args, **kwargs)

        def __call__(self, parser, namespace, values, option_string=None):
            values = (getattr(namespace, self.dest)
                      + (tuple(values.split(",")) if values else ()))
            try:
                idx = values.index("")
            except ValueError:
                pass
            else:
                values = values[idx + 1:]
            setattr(namespace, self.dest, values)

    parser = ArgumentParser(
        description="Create a PKGBUILD for a PyPI package and run makepkg.",
        formatter_class=type("", (RawDescriptionHelpFormatter,
                                  ArgumentDefaultsHelpFormatter), {}),
        epilog="Arguments documented as comma-separated lists can be passed "
               "multiple times; an empty value can be used to strip out "
               "values passed so far.")
    parser.add_argument("--version", action="version",
                        version="%(prog)s {}".format(__version__))
    parser.add_argument(
        "names", metavar="name", nargs="*",
        help="The PyPI package names.")
    parser.add_argument(
        "-v", "--verbose", action="store_true", default=False,
        help="Log at DEBUG level.")
    parser.add_argument(
        "-o", "--outdated", action="store_true", default=False,
        help="Find outdated packages.")
    parser.add_argument(
        "-u", "--update", action="store_true", default=False,
        help="Find and build outdated packages.")
    parser.add_argument(
        "-i", "--ignore", metavar="NAME,...", action=CommaSeparatedList,
        help="Comma-separated list of packages not to be updated.")
    parser.add_argument(
        "-b", "--base-path", type=Path, default=Path(),
        help="Base path where the packages folders are created.")
    parser.add_argument(
        "-f", "--force", action="store_true",
        help="Overwrite a previously existing PKGBUILD.")
    parser.add_argument(
        "--pre", action="store_true",
        help="Include pre-releases.")
    parser.add_argument(
        "-r", "--pkgrel", default="00",
        help="Force value of $pkgrel (not applicable to metapackages).  "
             "Set e.g. to 99 to override AUR packages.")
    parser.add_argument(
        "-g", "--guess-makedepends", metavar="MAKEDEPENDS,...",
        action=CommaSeparatedList, default=["cython", "swig"],
        help="Comma-separated list of makedepends that will be guessed.  "
             "Allowed values: cython, swig.")
    parser.add_argument(
        "-s", "--setup-requires", metavar="PYPI_NAME,...",
        action=CommaSeparatedList,
        help="Comma-separated list of setup_requires that will be forced.")
    parser.add_argument(
        "-t", "--pkgtypes",
        action=CommaSeparatedList,
        default=["anywheel", "sdist", "manylinuxwheel"],
        help="Comma-separated preference order for dists.")
    parser.add_argument(
        "-d", "--no-deps", action="store_false",
        dest="build_deps", default=True,
        help="Don't generate PKGBUILD for dependencies.")
    parser.add_argument(
        "-e", "--pkgbuild-extras", default="",
        help="Contents of PKGBUILD_EXTRAS.")
    parser.add_argument(
        "-m", "--makepkg", metavar="MAKEPKG_OPTS",
        default="--cleanbuild --nodeps",
        help="Additional arguments to pass to `makepkg`.")
    parser.add_argument(
        "-n", "--no-install", action="store_false",
        dest="install", default="True",
        help="Don't install the built packages.")
    parser.add_argument(
        "-p", "--pacman", metavar="PACMAN_OPTS",
        default="",
        help="Additional arguments to pass to `pacman -U`.")
    args = parser.parse_args()
    log_level = logging.DEBUG if vars(args).pop("verbose") else logging.INFO
    LOGGER.setLevel(log_level)
    logging.basicConfig(level=log_level)
    handler_level = logging.getLogger().handlers[0].level
    @LOGGER.addFilter
    def f(record):
        # This hack allows us to run with COLOREDLOGS_AUTO_INSTALL=1 without
        # having to set the root handler level to DEBUG (which would affect
        # other packages as well).  We still need to set this package's logger
        # level accordingly as otherwise DEBUG level records will not even
        # reach this filter.
        if record.levelno >= log_level:
            record.levelno = max(handler_level, record.levelno)
        return True

    # Dependency checking needs to happen after logging is configured.
    for cmd in ["namcap", "pkgfile"]:
        if shutil.which(cmd) is None:
            parser.error("Missing dependency: {}".format(cmd))
    try:
        _run_shell("pkgfile pkgfile >/dev/null")
    except CalledProcessError:
        # "error: No repo files found. Please run `pkgfile --update'."
        sys.exit(1)

    outdated, update, ignore, install, pacman_opts = map(
        vars(args).pop, ["outdated", "update", "ignore", "install", "pacman"])

    if outdated:
        if vars(args).pop("names"):
            parser.error("--outdated should be given with no name.")
        find_outdated()

    elif update:
        if vars(args).pop("names"):
            parser.error("--update-outdated should be given with no name.")
        ignore = {*map(distlib_normalize_name, ignore)}
        names = {distlib_normalize_name(name)
                 for name, *_ in map(str.split,
                                     sum(find_outdated().values(), []))}
        ignored = ignore & names
        if ignored:
            LOGGER.info("Ignoring update of %s.", ", ".join(sorted(ignored)))
        for name in sorted(names - ignore):
            try:
                create_package(name, Options(**vars(args), is_dep=False))
            except PackagingError as exc:
                LOGGER.error("%s", exc)
                return 1

    else:
        if not args.names:
            parser.error("the following arguments are required: name")
        try:
            for name in vars(args).pop("names"):
                create_package(name, Options(**vars(args), is_dep=False))
        except PackagingError as exc:
            LOGGER.error("%s", exc)
            return 1

    cmd = ""
    if install and Package.build_cache:
        cmd += "pacman -U{} {} {}".format(
            "" if args.build_deps else "dd",
            pacman_opts,
            " ".join(
                str(fpath) for fpath, is_dep in Package.build_cache.values()))
        deps = [name for name, (fpath, is_dep) in Package.build_cache.items()
                if is_dep]
        if deps:
            cmd += "; pacman -D --asdeps {}".format(" ".join(deps))
        cmd = "sudo sh -c {}".format(shlex.quote(cmd))
        _run_shell(cmd, check=False, verbose=True)


if __name__ == "__main__":
    sys.exit(main())
