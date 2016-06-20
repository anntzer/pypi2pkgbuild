PyPI2PKGBUILD
=============

Convert PyPI entries to Arch Linux PKGBUILDs, inspired from
[pip2arch](https://github.com/bluepeppers/pip2arch).

`pypi2pkgbuild.py PYPINAME` creates a PKGBUILD (in a git repo) for the given
PyPI package.  Because PyPI's dependencies are somewhat unreliable, it installs
the package in a virtualenv to figure out the dependencies.

The package is then built and verified with `namcap`.

Improvements over pip2arch
--------------------------

- Supports wheels (if both a sdist and a wheel is available, prefer the former,
  except if `--prefer-wheel` is passed).
- Resolves Python dependencies via installation in a temporary virtualenv, and
  also creates PKGBUILDs for those that are not available as official packages.
- Resolves binary dependencies via `namcap` and adds them to the `depends`
  array if they are installed (thus, it is suggested to first install them as
  `--asdeps` and then let the generated PKGBUILD pick them up as dependencies).
- Automatically tries to fetch a missing license file from Github, if
  applicable.
- Automatically builds the package (with options given in `--makepkg=...`) and
  run `namcap`.

The goal is to make this tool as automated as possible: if all the information
to build a package is (reasonably) accessible, this tool should be able to
build it.

In order to provide additional information to `makepkg`, edit
`PKGBUILD_EXTRAS`, which is sourced at the *end* of `PKGBUILD`.

Dependencies
------------

- Python 3.5+
- `pkgfile` (to check which dependencies are already available as official
  packages)
