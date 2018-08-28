#!/usr/bin/env python
"""Convert PyPI entries to Arch Linux packages."""

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

import pkg_resources

try:
    import setuptools_scm
    __version__ = setuptools_scm.get_version(  # xref setup.py
        root=".", relative_to=__file__,
        version_scheme="post-release", local_scheme="node-and-date")
except (ImportError, LookupError):
    try:
        __version__ = pkg_resources.get_distribution("pypi2pkgbuild").version
    except pkg_resources.DistributionNotFound:
        __version__ = "(unknown version)"


LOGGER = logging.getLogger(Path(__file__).stem)

PY_TAGS = ["py{0.major}".format(sys.version_info),
           "cp{0.major}".format(sys.version_info),
           "py{0.major}{0.minor}".format(sys.version_info),
           "cp{0.major}{0.minor}".format(sys.version_info)]
PLATFORM_TAGS = {
    "any": "any", "manylinux1_i686": "i686", "manylinux1_x86_64": "x86_64"}
THIS_ARCH = ["i686", "x86_64"][sys.maxsize > 2 ** 32]
SDIST_SUFFIXES = [".tar.gz", ".tgz", ".tar.bz2", ".zip"]
LICENSE_NAMES = ["LICENSE", "LICENSE.txt", "license.txt",
                 "COPYING", "COPYING.md", "COPYING.rst", "COPYING.txt",
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
    "Boost Software License 1.0 (BSL-1.0)":
        "Boost",
    # "CCPL",
    "Common Development and Distribution License 1.0 (CDDL-1.0)":
        "CDDL",
    "Eclipse Public License 1.0 (EPL-1.0)":
        "EPL",
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
        "MPL2",
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
# Maintainer: {config[PACKAGER]}

export PIP_CONFIG_FILE=/dev/null
export PIP_DISABLE_PIP_VERSION_CHECK=true

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
source=(PKGBUILD_EXTRAS)
md5sums=(SKIP)
"""

SDIST_SOURCE = """\
source+=({url[url]})
md5sums+=({url[md5_digest]})
"""

WHEEL_ANY_SOURCE = """\
source+=({url[url]})
md5sums+=({url[md5_digest]})
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
_first_source() {
    echo " ${source_i686[@]} ${source_x86_64[@]} ${source[@]}" |
        tr ' ' '\\n' | grep -Pv '^(PKGBUILD_EXTRAS)?$' | head -1
}

if [[ $(_first_source) =~ ^git+ ]]; then
    provides+=("${pkgname%-git}")
    conflicts+=("${pkgname%-git}")
fi

_is_wheel() {
    [[ $(_first_source) =~ \\.whl$ ]]
}

_dist_name() {
    basename "$(_first_source)" |
      sed 's/\\(""" + re.escape("|".join(SDIST_SUFFIXES)) + """\\|\\.git\\)$//'
}

if [[ $(_first_source) =~ ^git+ ]]; then
    _pkgver() {
        ( set -o pipefail
          cd "$srcdir/$(_dist_name)"
          git describe --long --tags 2>/dev/null |
            sed 's/^v//;s/\\([^-]*-g\\)/r\\1/;s/-/./g' ||
          printf "r%s.%s" \\
              "$(git rev-list --count HEAD)" "$(git rev-parse --short HEAD)"
        )
    }

    pkgver() { _pkgver; }
fi

_build() {
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
    # Build the wheel (which we allow to fail) only after fetching the license.
    /usr/bin/pip wheel -v --no-deps --wheel-dir="$srcdir" \\
        --global-option=--no-user-cfg \\
        --global-option=build --global-option=-j"$(nproc)" . ||
        true
}

build() { _build; }

_check() {
    # Define check(), possibly using _check as a helper, to run the tests.
    # You may need to call `python setup.py build_ext -i` first.
    if _is_wheel; then return; fi
    cd "$srcdir/$(_dist_name)"
    /usr/bin/python setup.py -q test
}

_package() {
    cd "$srcdir"
    # pypa/pip#3063: pip always checks for a globally installed version.
    /usr/bin/pip --quiet install --root="$pkgdir" \\
        --no-deps --ignore-installed --no-warn-script-location \\
        "$(ls ./*.whl 2>/dev/null || echo ./"$(_dist_name)")"
    if [[ -f LICENSE ]]; then
        install -D -m644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
    fi
}

package() { _package; }

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

    Log at ``DEBUG`` level except if the *verbose* kwarg is set, in which case
    log at ``INFO`` level.
    """
    kwargs = {"shell": isinstance(args, str),
              "env": {**os.environ,
                      # This should fallback to C if the locale is not present.
                      # We'd prefer C.utf8 but that doesn't exist.  With other
                      # locales, outputs cannot be parsed.
                      "LC_ALL": "en_US.utf8",
                      "PYTHONNOUSERSITE": "1",
                      "PIP_CONFIG_FILE": "/dev/null",
                      "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                      **kwargs.pop("env", {})},
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
    cproc = subprocess.run(args, **kwargs)
    # Stripping final newlines matches the behavior of `a=$(foo)`.
    if isinstance(cproc.stdout, str):
        cproc.stdout = cproc.stdout.rstrip("\n")
    elif isinstance(cproc.stdout, bytes):
        cproc.stdout = cproc.stdout.rstrip(b"\n")
    return cproc


@lru_cache()
def get_makepkg_conf():
    with TemporaryDirectory() as tmpdir:
        mini_pkgbuild = textwrap.dedent(r"""
            pkgname=_
            pkgver=0
            pkgrel=0
            arch=(any)
            prepare() {
                printf "CFLAGS %s\0CXXFLAGS %s\0PACKAGER %s" \
                    "$CFLAGS" "$CXXFLAGS" "$PACKAGER"; exit 0;
            }
        """)
        Path(tmpdir, "PKGBUILD").write_text(mini_pkgbuild)
        try:
            out = _run_shell(
                "makepkg", cwd=tmpdir, stdout=PIPE, stderr=PIPE).stdout
        except CalledProcessError as e:
            sys.stderr.write(e.stderr)
            raise
    return dict(pair.split(" ", 1) for pair in out.split("\0"))


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
        namedtuple("_WheelInfo", "name version build pythons abi platform")):
    @classmethod
    def parse(cls, url):
        parts = Path(urllib.parse.urlparse(url).path).stem.split("-")
        if len(parts) == 5:
            name, version, pythons, abi, platform = parts
            build = ""
        elif len(parts) == 6:
            name, version, build, pythons, abi, platform = parts
        else:
            raise ValueError(f"Invalid wheel url: {url}")
        return cls(
            name, version, build, set(pythons.split(".")), abi, platform)


# Copy-pasted from PEP503.
def pep503_normalize_name(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def to_wheel_name(pep503_name):
    return pep503_name.replace("-", "_")


class PackagingError(Exception):
    pass


# Vendored from pip._internal.vcs.VersionControl.get_url_rev.
def _vcs_get_url_rev(url):
    error_message = (
        "Sorry, '%s' is a malformed VCS url. "
        "The format is <vcs>+<protocol>://<url>, "
        "e.g. svn+http://myrepo/svn/MyApp#egg=MyApp"
    )
    assert '+' in url, error_message % url
    url = url.split('+', 1)[1]
    scheme, netloc, path, query, frag = urllib.parse.urlsplit(url)
    rev = None
    if '@' in path:
        path, rev = path.rsplit('@', 1)
    url = urllib.parse.urlunsplit((scheme, netloc, path, query, ''))
    return url, rev


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
            raise PackagingError(f"Failed to download {parsed.netloc}, "
                                 "possibly due to a buggy setup.py")
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
    makedepends = [PackageRef("pip"), PackageRef("wheel")]
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
            # Don't stay in source folder, which may contain wheels/sdists/etc.
            cd {venvdir}
            . '{venvdir}/bin/activate'
            pip install --upgrade {setup_requires} >/dev/null
            install_cmd() {{
                pip freeze | cut -d= -f1 | sort >'{venvdir}/pre_install_list'
                if ! pip install --no-deps '{req}'; then
                    return 1
                fi
                pip freeze | cut -d= -f1 | sort >'{venvdir}/post_install_list'
                # installed name, or real name if it doesn't appear
                # (setuptools, pip, Cython, numpy).
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
                python - "$(pip show -v "$install_name")" <<EOF
            from email.parser import Parser
            import json
            import sys
            print(json.dumps(dict(Parser().parsestr(sys.argv[1]))))
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
            process = _run_shell(
                script, stdout=PIPE, env={
                    # Matters, as a built wheel would get cached.
                    "CFLAGS": get_makepkg_conf()["CFLAGS"],
                    # Not actually used, per pypa/setuptools#1192.  Still
                    # relevant for packages that ship their own autoconf-based
                    # builds, e.g. wxPython.
                    "CXXFLAGS": get_makepkg_conf()["CXXFLAGS"],
                })
        except CalledProcessError:
            sys.stderr.write(log.read())
            raise PackagingError(f"Failed to obtain metadata for {name}.")
        more_requires = more_requires_log.read().splitlines()
    metadata = {k.lower(): v for k, v in json.loads(process.stdout).items()}
    metadata["requires"] = [
        *(metadata["requires"].split(", ") if metadata["requires"] else []),
        *more_requires]
    metadata["classifiers"] = metadata["classifiers"].split("\n  ")[1:]
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
        url, rev = _vcs_get_url_rev(name)
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
                f"https://pypi.org/pypi/{name}/{_version}/json"
                if _version else f"https://pypi.org/pypi/{name}/json")
        except urllib.error.HTTPError:
            return
        # Load as OrderedDict so that always the same sdist is chosen if e.g.
        # both zip and tgz are available.
        request = json.loads(r.read(), object_pairs_hook=OrderedDict)
        if not _version:
            versions = [
                version for version in map(pkg_resources.parse_version,
                                           request["releases"])
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
        info = locals()[f"_get_info_{source}"]()
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
            "(shopt -s nocaseglob; pacman -Qo {}/{}-*-info 2>/dev/null) | "
            "rev | cut -d' ' -f1,2 | rev".format(
                _get_site_packages_location(), to_wheel_name(pep503_name)),
            stdout=PIPE).stdout.split()
        or _run_shell(
            "pacman -Q python-{} 2>/dev/null".format(pep503_name),
            stdout=PIPE, check=False).stdout.split())
    if parts:
        pkgname, version = parts  # This will raise if there is an ambiguity.
        if pkgname.endswith("-git"):
            expected_conflict = pkgname[:-len("-git")]
            if _run_shell(
                    f"pacman -Qi {pkgname} 2>/dev/null | "
                    f"grep -q 'Conflicts With *: {expected_conflict}$'",
                    check=False).returncode == 0:
                pkgname = pkgname[:-len("-git")]
            else:
                raise PackagingError(
                    f"Found installed package {pkgname} which does NOT "
                    f"conflict with {expected_conflict}; please uninstall it "
                    f"first.")
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
            r"{wheel_name}-.*py{version.major}\.{version.minor}\.egg-info' | "
            "cut -f1 | uniq | cut -d/ -f2".format(
                parent="site-packages/" if standalone else "",
                wheel_name=to_wheel_name(pep503_name),
                version=sys.version_info),
            stdout=PIPE).stdout.splitlines())
        if len(candidates) > 1:
            message = "Multiple candidates for {}: {}.".format(
                pep503_name, ", ".join(candidates))
            try:
                canonical, = (
                    candidate for candidate in candidates
                    if candidate.startswith(f"python-{pep503_name} "))
            except ValueError:
                raise PackagingError(message)
            else:
                LOGGER.warning("%s  Using canonical name: %s.",
                               message, canonical.split()[0])
                candidates = [canonical]
        if len(candidates) == 1:
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
        self.pep503_name = pep503_normalize_name(self.pypi_name)

        if subpkg_of:
            pkgname = f"python--{self.pep503_name}"
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
            default = f"python-{self.pep503_name}", None
            pkgname, arch_version = installed or arch or default
            depname, _ = arch or installed or default

        arch_packaged = _run_shell(
            "pkgfile -l {} 2>/dev/null | "
            # Package name has no dash (per packaging standard) nor slashes
            # (which can occur when a subpackage is vendored (depending on how
            # it is done), e.g. `.../foo.egg-info` and `.../foo/bar.egg-info`
            # both existing).
            r"grep -Po '(?<=site-packages/)[^-/]*(?=.*\.egg-info/?$)'".
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


BuildCacheEntry = namedtuple(
    "BuildCacheEntry", "pkgname path is_dep namcap_report")


class _BasePackage(ABC):
    build_cache = []

    def __init__(self):
        self._files = OrderedDict()
        # self._pkgbuild = ...

    @abc.abstractmethod
    def write_deps_to(self, options):
        pass

    def get_pkgbuild_extras(self, options):
        if os.path.isdir(options.pkgbuild_extras):
            extras_path = Path(options.pkgbuild_extras,
                               f"{self.pkgname}.PKGBUILD_EXTRAS")
            if extras_path.exists():
                LOGGER.info("Using %s.", extras_path)
                return extras_path.read_text()
            else:
                return ""
        else:
            return options.pkgbuild_extras

    def write_to(self, options):
        cwd = options.base_path / self.pkgname
        cwd.mkdir(parents=True, exist_ok=options.force)
        (cwd / "PKGBUILD").write_text(self._pkgbuild)
        (cwd / "PKGBUILD_EXTRAS").write_text(self.get_pkgbuild_extras(options))
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
            return Path(_run_shell("makepkg --packagelist",
                                   cwd=cwd, stdout=PIPE).stdout)

        fullpath = _get_fullpath()
        # Update PKGBUILD.
        needs_rebuild = False
        namcap = (_run_shell(["namcap", fullpath.name], cwd=cwd, stdout=PIPE)
                  .stdout.splitlines())
        # `pkgver()` may update the PKGBUILD, so reread it.
        pkgbuild_contents = (cwd / "PKGBUILD").read_text()
        # Binary dependencies.
        extra_deps_re = (f"(?<=^{self.pkgname} "
                         "E: Dependency ).*(?= detected and not included)")
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
        any_arch_re = (f"^{self.pkgname} "
                       "E: ELF file .* found in an 'any' package.")
        if any(re.search(any_arch_re, line) for line in namcap):
            pkgbuild_contents = re.sub(
                "(?m)^arch=.*$", f"arch=({THIS_ARCH})", pkgbuild_contents, 1)
            needs_rebuild = True
        if needs_rebuild:
            # Remove previous package, repackage, and get new name (arch may
            # have changed).
            fullpath.unlink()
            (cwd / "PKGBUILD").write_text(pkgbuild_contents)
            _run_shell("makepkg --force --repackage --nodeps", cwd=cwd)
            fullpath = _get_fullpath()
        namcap_pkgbuild_report = _run_shell(
            "namcap PKGBUILD", cwd=cwd, stdout=PIPE, check=False).stdout
        # Suppressed namcap warnings (may be better to do this via a namcap
        # option?):
        # - Python dependencies always get misanalyzed; filter them away.
        # - Dependencies match install_requires + whatever namcap wants us to
        #   add, so suppress warning about redundant transitive dependencies.
        # - Extension modules unconditionally link to `libpthread` (see
        #   output of `python-config --libs`); filter that away.
        # - Extension modules appear to never be PIE?
        namcap_package_report = _run_shell(
            f"namcap {fullpath.name} | "
            f"grep -v \"^{self.pkgname} W: "
                r"\(Dependency included and not needed"
                r"\|Dependency .* included but already satisfied$"
                r"\|Unused shared library '/usr/lib/libpthread\.so\.0' by"
                r"\|ELF file .* lacks PIE\.$\)"
            "\"", cwd=cwd, stdout=PIPE, check=False).stdout
        namcap_report = [
            line for report in [namcap_pkgbuild_report, namcap_package_report]
            for line in report.split("\n") if line]
        if re.search(f"^{fullpath.name} E: ", namcap_package_report):
            raise PackagingError("namcap found a problem with the package.")
        _run_shell("makepkg --printsrcinfo >.SRCINFO", cwd=cwd)
        type(self).build_cache.append(BuildCacheEntry(
            self.pkgname, fullpath, options.is_dep, namcap_report))
        # FIXME Suppress message about redundancy of 'python' dependency.


class Package(_BasePackage):
    def __init__(self, ref, options):
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
                f"No URL available for package {self.pkgname}.")

        self._find_arch_and_makedepends(options)
        for dep in self._makedepends:
            if _run_shell(f"pacman -Q {dep.pkgname} >/dev/null 2>&1",
                          check=False).returncode:
                # Only log this as needed, to not spam messages about pip.
                _run_shell(f"sudo pacman -S --asdeps {dep.pkgname}",
                           verbose=True)
        self._extract_setup_requires()

        metadata = _get_metadata(
            f"{ref.orig_name}=={self.pkgver}"
            if urllib.parse.urlparse(ref.orig_name).scheme == ""
            else ref.orig_name,
            self._makedepends.pep503_names)
        self._depends = DependsTuple(
            PackageRef(req)
            if options.build_deps else
            # FIXME Could use something slightly better, i.e. still check local
            # packages...
            NonPyPackageRef("python-{}".format(pep503_normalize_name(req)))
            for req in metadata["requires"])
        self._licenses = self._find_license()

        stream.write(
            PKGBUILD_HEADER.format(pkg=self, config=get_makepkg_conf()))
        if self._urls[0]["packagetype"] == "bdist_wheel":
            # Either just "any", or some specific archs.
            for url in self._urls:
                if url["packagetype"] != "bdist_wheel":
                    continue
                wheel_info = WheelInfo.parse(url["url"])
                if wheel_info.platform == "any":
                    src_template = WHEEL_ANY_SOURCE
                else:
                    src_template = WHEEL_ARCH_SOURCE
                stream.write(src_template.format(
                    arch=PLATFORM_TAGS[wheel_info.platform],
                    url=url,
                    name=Path(urllib.parse.urlparse(url["url"]).path).name))
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
                wheel_info = WheelInfo.parse(url["url"])
                if not wheel_info.pythons.intersection(PY_TAGS):
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

    def _find_arch_and_makedepends(self, options):
        if self._get_first_package_type() == "bdist_wheel":
            self._arch = sorted(
                {PLATFORM_TAGS[WheelInfo.parse(url["url"]).platform]
                 for url in self._urls if url["packagetype"] == "bdist_wheel"})
            self._makedepends = DependsTuple(
                map(PackageRef, ["pip", *options.setup_requires]))
        else:
            self._arch = ["any"]
            self._makedepends = DependsTuple((
                *map(PackageRef, options.setup_requires),
                *_guess_url_makedepends(
                    self._get_sdist_url(), options.guess_makedepends)))
        with TemporaryDirectory() as tmpdir:
            Path(tmpdir, "PKGBUILD").write_text(
                # makepkg always requires that these three variables are set.
                f"pkgname={self.pkgname}\n"
                f"pkgver={self.pkgver}\n"
                f"pkgrel={self.pkgrel}\n"
                + self.get_pkgbuild_extras(options))
            extra_makedepends = _run_shell(
                r"makepkg --printsrcinfo | "
                r"grep -Po '(?<=^\tmakedepends = ).*'",
                cwd=tmpdir, stdout=PIPE, check=False).stdout
            if extra_makedepends:
                self._makedepends = DependsTuple(
                    [*self._makedepends,
                     # Use NonPyPackageRef even when the extra makedepends is
                     # actually a Python package, because we need access to it
                     # (as a system package) from within the build venv.
                     *map(NonPyPackageRef, extra_makedepends.split("\n"))])

    def _extract_setup_requires(self):
        makedepends = []
        for pkg in self._makedepends:
            if isinstance(pkg, PackageRef):
                makedepends.append(pkg)
            elif isinstance(pkg, NonPyPackageRef):
                pep503_name = _run_shell(
                    f"pacman -Qql {pkg.pkgname} | "
                    f"grep -Po '(?<=^{_get_site_packages_location()}/)"
                    r"[^-]*(?=-.*\.(dist|egg)-info/$)'",
                    stdout=PIPE, check=False).stdout
                makedepends.append(
                    PackageRef(pep503_name) if pep503_name else pkg)
            else:
                raise TypeError("Unexpected makedepends entry")
        self._makedepends = DependsTuple(makedepends)

    def _find_license(self):
        # FIXME Support license-in-wheel.
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
                    licenses.append(f"custom:{license_class}")
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
                # Could instead lookup
                #   https://api.github.com/repos/:owner/:repo/contents
                # or see opengh/github-ls.
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
                try:
                    sdist_unpackacked_path = _get_url_unpacked_path_or_null(
                        self._get_sdist_url())
                    # Should really fail with CalledProcessError (e.g. if
                    # wheel-only) but that can actually be transformed into a
                    # PackagingError; see _get_url_impl for explanation...
                except PackagingError:
                    pass
                else:
                    for path in map(sdist_unpackacked_path.joinpath,
                                    LICENSE_NAMES):
                        if path.is_file():
                            self._files.update(LICENSE=path.read_bytes())
                            _license_found = True
                            break
            if not _license_found:
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
            return f"{name}={self.pkgver}"

    def write_deps_to(self, options):
        for ref in self._depends:
            if not ref.exists:
                # Dependency not found, build it too.
                create_package(ref.pep503_name, options._replace(is_dep=True))


class MetaPackage(_BasePackage):
    def __init__(self, ref, options):
        super().__init__()
        self._ref = ref
        self._arch_version = self._ref.arch_version._replace(
            pkgrel=self._ref.arch_version.pkgrel + ".99")
        self._subpkgrefs = DependsTuple(
            PackageRef(name, subpkg_of=ref, pre=options.pre)
            for name in ref.arch_packaged)
        self._subpkgs = [Package(ref, options) for ref in self._subpkgrefs]
        for pkg in self._subpkgs:
            pkg._pkgbuild = re.sub(
                "(?m)^conflicts=.*$",
                "conflicts=('{0}<{1}' '{0}>{1}')".format(
                    ref.pkgname, self._arch_version),
                pkg._pkgbuild,
                1)
        self._pkgbuild = (
            PKGBUILD_HEADER.format(pkg=self, config=get_makepkg_conf())
            + METAPKGBUILD_CONTENTS)

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


def dispatch_package_builder(name, options):
    ref = PackageRef(
        name, pre=options.pre, guess_makedepends=options.guess_makedepends)
    cls = Package if len(ref.arch_packaged) <= 1 else MetaPackage
    return cls(ref, options)


@lru_cache()
def create_package(name, options):
    pkg = dispatch_package_builder(name, options)
    if options.build_deps:
        pkg.write_deps_to(options)
    pkg.write_to(options)


def find_outdated():
    syswide_location = (
        "{0.prefix}/lib/python{0.version_info.major}.{0.version_info.minor}"
        "/site-packages".format(sys))
    outdated = json.loads(
        _run_shell("pip list --outdated --format=json", stdout=PIPE).stdout)
    if not outdated:
        return {}
    # `pip show` is rather slow, so just call it once.
    locs = _run_shell(
        "pip show {} 2>/dev/null | grep -Po '(?<=^Location: ).*'".
        format(" ".join(row["name"] for row in outdated)),
        stdout=PIPE).stdout.splitlines()
    owners = {}
    for row, loc in zip(outdated, locs):
        if loc == syswide_location:
            pkgname, arch_version = _find_installed_name_version(row["name"])
            # Check that pypi's version is indeed newer.  Some packages
            # mis-report their version to pip (e.g., slicerator 0.9.7's Github
            # release).
            if arch_version.pkgver == row["latest_version"]:
                LOGGER.warning(
                    "pip thinks that %s is outdated, but the installed "
                    "version is actually %s, and up-to-date.",
                    row["name"], row["latest_version"])
                continue
            owners.setdefault(f"{pkgname} {arch_version}", []).append(row)
    owners = OrderedDict(sorted(owners.items()))
    rows = sum(owners.values(), [])
    name_len, ver_len, lver_len, lft_len = (
        max(map(len, (row[key] for row in rows)))
        for key in ["name", "version", "latest_version", "latest_filetype"])
    for owner, rows in owners.items():
        print(owner)
        for row in rows:
            print("    "
                  f"{row['name']:{name_len}} "
                  f"{row['version']:{ver_len}} -> "
                  f"{row['latest_version']:{lver_len}} "
                  f"({row['latest_filetype']:{lft_len}})")
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
                        version=f"%(prog)s {__version__}")
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
        "-u", "--upgrade", action="store_true", default=False,
        help="Find and build outdated packages.")
    parser.add_argument(
        "-i", "--ignore", metavar="NAME,...", action=CommaSeparatedList,
        help="Comma-separated list of packages not to be upgrade.")
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
        help="Either contents of PKGBUILD_EXTRAS, or path to a patch "
             "directory (if a valid path).  A patch directory should contain "
             "files of the form $pkgname.PKGBUILD_EXTRAS, which are used as "
             "PKGBUILD_EXTRAS.")
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
            parser.error(f"Missing dependency: {cmd}")
    try:
        _run_shell("pkgfile pkgfile >/dev/null")
    except CalledProcessError:
        # "error: No repo files found. Please run `pkgfile --update'."
        sys.exit(1)

    outdated, upgrade, ignore, install, pacman_opts = map(
        vars(args).pop, ["outdated", "upgrade", "ignore", "install", "pacman"])

    if outdated:
        if vars(args).pop("names"):
            parser.error("--outdated should be given with no name.")
        find_outdated()

    elif upgrade:
        if vars(args).pop("names"):
            parser.error("--upgrade-outdated should be given with no name.")
        ignore = {*map(pep503_normalize_name, ignore)}
        names = {pep503_normalize_name(row["name"])
                 for row in sum(find_outdated().values(), [])}
        ignored = ignore & names
        if ignored:
            LOGGER.info("Ignoring upgrade of %s.", ", ".join(sorted(ignored)))
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

    print("\n".join(line for cache_entry in Package.build_cache
                    for line in cache_entry.namcap_report))

    if install and Package.build_cache:
        cmd = "pacman -U{} {} {}".format(
            "" if args.build_deps else "dd",
            pacman_opts,
            " ".join(shlex.quote(str(cache_entry.path))
                     for cache_entry in Package.build_cache))
        deps = [cache_entry.pkgname for cache_entry in Package.build_cache
                if cache_entry.is_dep]
        if deps:
            cmd += "; pacman -D --asdeps {}".format(" ".join(deps))
        cmd = "sudo sh -c {}".format(shlex.quote(cmd))
        _run_shell(cmd, check=False, verbose=True)


if __name__ == "__main__":
    sys.exit(main())
