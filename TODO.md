Issues
======

- PyPI packages that depends on another package's `extra_requires` are not
  supported (needs upstream support from `pip show`).
    - `scikit-image` depends on `dask[array]`.

- License support is incomplete.
    - e.g. `matplotlib` has a `LICENSE` *folder*.

- `git` packages are cloned twice; we may be able to cache them.

- Meta packages are fully rebuilt even if only a component needs to be built.

- `scipy` fails to build, probably due to numpy/numpy#7779 (`LDFLAGS` set by
  `makepkg` strips defaults).  Setting `LDFLAGS` to `"$(. /etc/makepkg.conf;
  echo $LDFLAGS) -shared"` does not seem to help, though.

Arch packaging
==============

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
