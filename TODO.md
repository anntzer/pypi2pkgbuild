Issues
======

- PyPI packages that depends on another package's `extra_requires`.
  Needs upstream support from `pip show`.
    - `scikit-image` depends on `dask[array]`.

- Install `Cython` itself (`numpy` is probably too complicated).

- Arch packages that "vendor" some dependencies are supported, although the
  `depends` array may be a bit mangled.

- Incomplete support for licences.
    - e.g. `matplotlib` has a `LICENSE` *folder*.

Remaining manual packages
=========================

- Licensing issues:
    - `versioneer`: no classifier (public domain)

- Undeclared dependencies:
    - `hmmlearn`
    - `nitime` (still uses `distutils`...)
    - `supersmoother`

- `ctypes`-loaded binary dependencies.
    - `yep`: depends on `gperftools`
