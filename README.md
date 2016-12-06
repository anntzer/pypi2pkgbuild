PyPI2PKGBUILD
=============

Convert PyPI entries to Arch Linux packages, inspired from
[pip2arch](https://github.com/bluepeppers/pip2arch).

`pypi2pkgbuild.py PYPINAME` creates a PKGBUILD (in a git repo) for the latest
version of the given PyPI package (including prereleases if the `--pre` flag is
passed.  Because PyPI's dependencies are somewhat unreliable, it installs the
package in a virtualenv to figure out the dependencies.

A `-git` package can be built with `pypi2pkbguild.py git+https://...`.

The package is then built and verified with `namcap`.

The goal is to make this tool as automated as possible: if all the information
to build a package is (reasonably) accessible, this tool should be able to
build it.

In order to provide additional information to `makepkg`, edit
`PKGBUILD_EXTRAS`, which is sourced at the *end* of `PKGBUILD`.

By default, `pkgrel` is set to `00`.  The intent is to prefer native packages
(or AUR ones, if using an AUR helper), relying on this tool only for
missing/out-of-date packages.  Use the `--pkgrel` flag to set it to another
value, e.g. `99` to override native and AUR packages.

Improvements over pip2arch
--------------------------

- Supports wheels (the default is to prefer `any`-platform wheels, then
  `sdist`s, then `manylinux1` wheels, but this can be changed using
  `--pkgtypes`).
- Resolves Python dependencies via installation in a temporary virtualenv, and
  also creates PKGBUILDs for those that are not available as official packages.
- Resolves binary dependencies via `namcap` and adds them to the `depends`
  array if they are installed (thus, it is suggested to first install them as
  `--asdeps` and then let the generated PKGBUILD pick them up as dependencies).
- Automatically tries to fetch a missing license file from Github, if
  applicable.
- Automatically builds the package (with options given in `--makepkg=...`) and
  run `namcap`.
- Automatically builds all outdated dependencies via `-O`.

Build-time dependencies
-----------------------

`pypi2pkgbuild.py` attempts to guess whether `Cython` and `SWIG` are build-time
dependencies by checking for the presence of `.pyx` and `.i` files,
respectively.  If this is not desired, set the `--guess-makedepends` option
accordingly.

`pypi2pkgbuild.py` guesses whether `numpy` is a build-time dependency by
attempting a build without `numpy`, then, in case of failure, a build with
`numpy`.

Vendored packages
-----------------

Some Arch packages (e.g. `ipython`) include a number of smaller PyPI packages.

Because it is not possible to assign a meaningful version automatically,
`pypi2pkgbuild.py` instead creates an independent Arch package for each of
the PyPI packages (with two dashes in the name, to prevent name conflicts)
and a master package (with `pkgrel` equal to the upstream `pkgrel.99`) that
depends on all of them.  All these packages `conflict` with all versions of the
upstream package (except the newly created package), so updating should work
fine when the upstream package is actually updated.

However, dependencies are still expressed using the master package, so
internal dependencies will appear be circular.

All the packages are placed in a subfolder named `meta:$pkgname`, so one can
easily install everything by `cd`'ing there and running
```
    $ sudo pacman -U --asdeps **/*.xz
    $ sudo pacman -D --asexplicit $pkgname/$pkgname.tar.xz
```

Dependencies
------------

- Python 3.5+
- `pkgfile` (to check which dependencies are already available as official
  packages)

Installation
------------

`pip install .`, or just run the script directly.

You can even run PyPI2PKGBUILD on itself to create a proper Arch package
(`pypi2pkgbuild.py git+https://github.com/anntzer/pypi2pkgbuild`)...

Configuration
-------------

It is suggested to create an alias with standard options set, e.g.
```
    alias pypi2pkgbuild.py='PKGEXT=.pkg.tar pypi2pkgbuild.py -g cython -b /tmp/pypi2pkgbuild/ -f'
```
