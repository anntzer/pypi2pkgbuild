Issues
======

- Packages that are manually vendored (... poorly) by Arch (e.g., ``html5lib``
  into ``bleach``) cause some issues.  A solution would be to actually install
  the dependencies, check whether the last versions were installed, and error
  if this is not the case (indicating a requirement on an earlier version,
  which necessarily means manual vendoring).

- VCS fragments cannot be given.

- PyPI packages that depends on another package's ``extra_requires`` are not
  supported (needs upstream support from ``pip show``).

  - ``scikit-image`` depends on ``dask[array]``.

- License support is incomplete.

  - e.g. ``matplotlib`` has a ``LICENSE`` *folder*.

- Meta packages are fully rebuilt even if only a component needs to be built
  (although version dependencies -- in particular ``pkgrel``\s -- may have
  changed so it may not be possible to avoid this and maintain robustness).

- ``scipy`` fails to build, probably due to numpy/numpy#7779 (``LDFLAGS``
  set by ``makepkg`` strips defaults).  Setting ``LDFLAGS`` to ``"$(.
  /etc/makepkg.conf; echo $LDFLAGS) -shared"`` does not seem to help, though.

- ``fpm`` adds a ``get_metadata`` command to avoid having to install the
  package but this can't be done with e.g. wheels.  Perhaps we could hook
  something else?

- Move ``numpy`` support to ``--guess-makedepends``.

Arch packaging
==============

- Some packages are installed without an ``.egg-info`` (e.g. ``entrypoints``,
  ``PyQt5``) and thus not seen by ``pip list --outdated`` (and thus
  ``pypi2pkgbuild.py -o``).

Other mispackaged packages
==========================

- Bad vendoring by Arch.

  - ``bleach``

- Packages present with two names.

  - ``h5py`` (``python-h5py``, ``python-h5py-openmpi``).

- Setup-time non-Python dependencies.

  - ``notebook`` *from the git repository* (requires at least ``bower``,
    perhaps more).

- Setup-time dependencies (use ``--setup-requires=...`` as a workaround):

  - ``pomegranate`` (Cython files depend on scipy's BLAS ``pxd``\s.)

- Missing dependencies (use ``--pkgbuild-extras='depends+=(...)'`` as a
  workaround):

  - ``extras_requires`` (see above):

    - ``scikit-image`` (The AUR package doesn't even declare ``dask`` as a
      dependency.)

  - Undeclared dependencies:

    - ``hmmlearn`` (Fixed as of master.)
    - ``memory_profiler`` ("Strongly recommends" ``psutil``.)
    - ``sftpman-gtk`` (Depends on ``PyGObject``.)
    - ``sphinx-gallery`` (Could fetch ``requirements.txt`` from Github.)
    - ``supersmoother`` (Fixed as of master.)

  - ``ctypes``-loaded binary dependencies:

    - ``pylibftdi`` (Depends on ``libftdi``.)
    - ``yep`` (Depends on ``gperftools``.)

  - Wrappers for binaries:

    - ``graphviz``, ``sftpman``

- Undetected split packages:

  - Arch splits ``pygments`` into ``python-pygments`` and ``pygmentize``,
    but ``pypi2pkgbuild.py`` only sees the former (and thus does not
    provides/conflicts the latter).  ``pkgbase`` could be read out of
    ``.PKGINFO``, but does pacman provide a way to find packages given a
    ``pkgbase``?


