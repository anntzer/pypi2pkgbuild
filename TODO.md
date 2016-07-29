Issues
======

- PyPI packages that depends on another package's `extra_requires` are not
  supported (needs upstream support from `pip show`).
    - `scikit-image` depends on `dask[array]`.

- Special flags may be required (?) for optimized builds of `numpy` and `scipy`.

- License support is incomplete.
    - e.g. `matplotlib` has a `LICENSE` *folder*.

- `git` packages are cloned twice; we may be able to cache them.

Arch packaging
==============

- Arch packages that "vendor" some dependencies are supported, although the
  `depends` array may be a bit mangled.

- Some packages are not installed as wheels (e.g. PyQt5) and thus not seen by
  `pip list --outdated` (and thus `pypi2pkgbuild.py -o`).

Remaining manual packages
=========================

- Undeclared dependencies:
    - `hmmlearn`
    - `nitime` (still uses `distutils`...)
    - `supersmoother`

- `ctypes`-loaded binary dependencies.
    - `yep`: depends on `gperftools`
