Issues
======

- PyPI packages that depends on another package's `extra_requires`.
  Needs upstream support from `pip show`.
    - `scikit-image` depends on `dask[array]`.

- Arch packages that "vendor" some dependencies are supported, although the
  `depends` array may be a bit mangled.

- Complex license layouts.
  Can be worked around with `--no-license`.
    - `matplotlib` has a `LICENSE` *folder*.

Remaining manual packages
=========================

- Licensing issues:
    - `profilehooks`: need scraping to find link to Github (MIT)
    - `versioneer`: no classifier (public domain)

- Undeclared dependencies:
    - `gatspy`
    - `hmmlearn`
    - `nitime`
    - `supersmoother`

- `ctypes`-loaded binary dependencies.
    - `yep`: depends on `gperftools`
