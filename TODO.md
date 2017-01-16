Issues
======

- VCS fragments cannot be given.

- PyPI packages that depends on another package's `extra_requires` are not
  supported (needs upstream support from `pip show`).
    - `scikit-image` depends on `dask[array]`.

- License support is incomplete.
    - e.g. `matplotlib` has a `LICENSE` *folder*.

- Meta packages are fully rebuilt even if only a component needs to be built
  (although version dependencies -- in particular `pkgrel`s -- may have changed
  so it may not be possible to avoid this and maintain robustness).

- `scipy` fails to build, probably due to numpy/numpy#7779 (`LDFLAGS` set by
  `makepkg` strips defaults).  Setting `LDFLAGS` to `"$(. /etc/makepkg.conf;
  echo $LDFLAGS) -shared"` does not seem to help, though.

- `fpm` adds a `get_metadata` command to avoid having to install the package
  but this can't be done with e.g. wheels.  Perhaps we could hook something
  else?

- Move `numpy` support to `--guess-makedepends`.

Arch packaging
==============

- Some packages are installed without an `.egg-info` (e.g. `entrypoints`,
  `PyQt5`) and thus not seen by `pip list --outdated` (and thus
  `pypi2pkgbuild.py -o`).

Mispackaged packages
====================

- Setup-time dependencies (set `--setup-requires` correctly to work around):
    - `pomegranate` (Cython files depend on scipy's BLAS `pxd`s.)

- `extras_requires` (see above):
    - `scikit-image` (AUR package doesn't even declare `dask` as a dependency.)

- Undeclared dependencies:
    - `hmmlearn`
    - `memory_profiler` ("Strongly recommands" `psutil`.)
    - `nitime` (Still uses `distutils`...)
    - `sphinx-gallery` (Could fetch `requirements.txt` from Github.)
    - `supersmoother`

- `ctypes`-loaded binary dependencies:
    - `yep` (Depends on `gperftools`.)

- Wrappers for binaries:
    - `graphviz`
