Issues
======

- Arch packages that "vendor" some dependencies.
  Could be resolved by reproducing the vendoring.
    - `ipython` vendors `pickleshare`, etc.

- PyPI packages that depends on another package's `extra_requires`.
  Needs upstream support from `pip show`.
    - `scikit-image` depends on `dask[array]`.

- Complex license layouts.
    - `matplotlib` has a full `LICENSE` folder.

- Messed up names.
    - How am I supposed to know that `Cycler` is actually `cycler`?

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
