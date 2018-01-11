Issues
======

- Packages that are manually vendored (... poorly) by Arch (e.g., previously,
  ``html5lib`` into ``bleach``) cause some issues.  A solution would be to
  actually install the dependencies, check whether the last versions were
  installed, and error if this is not the case (indicating a requirement on an
  earlier version, which necessarily means manual vendoring).

- VCS fragments cannot be given.

- PyPI packages that depends on another package's ``extra_requires`` are not
  supported (needs upstream support from ``pip show``).

  - ``scikit-image`` depends on ``dask[array]``.

- License support is incomplete.

  - e.g. ``matplotlib`` has a ``LICENSE`` *folder*.
  - get licenses from wheels.

- Meta packages are fully rebuilt even if only a component needs to be built
  (although version dependencies -- in particular ``pkgrel``\s -- may have
  changed so it may not be possible to avoid this and maintain robustness).

- ``scipy`` fails to build, probably due to numpy/numpy#7779 (``LDFLAGS``
  set by ``makepkg`` strips defaults).  Setting ``LDFLAGS`` to ``"$(.
  /etc/makepkg.conf; echo $LDFLAGS) -shared"`` does not seem to help, though.

- ``fpm`` adds a ``get_metadata`` command to avoid having to install the
  package but this can't be done with e.g. wheels.  Perhaps we could hook
  something else?

- Move ``numpy`` support to ``--guess-makedepends``.  Implement
  ``guess-makedepends`` by adding shim files to the environment that check
  whether they are accessed.  A similar strategy can be used e.g. for swig,
  pybind11.

- Support non-Python makedepends other than ``swig``, e.g.

  - to build ``pygobject``, ``gobject-introspection`` and ``python-cairo`` must
    be installed first, **and** a ``setup_requires`` declared on ``pycairo``.
    (Additional conflicts issues with ``pygobject`` noted below.)

  - to build ``wxpython``, ``wxgtk3`` and ``webkit2gtk`` must be installed
    first.

Arch packaging
==============

- Some packages are installed without an ``.egg-info`` (e.g. ``entrypoints``,
  ``PyQt5``) and thus not seen by ``pip list --outdated`` (and thus
  ``pypi2pkgbuild.py -o``).

Other mispackaged packages
==========================

- Packages present with two names.

  - ``h5py`` (``python-h5py``, ``python-h5py-openmpi``).

- Setup-time non-Python dependencies.

  - ``notebook`` *from the git repository* (requires at least ``bower``,
    perhaps more).

- Undetected split packages:

  - Arch splits ``pygments`` into ``python-pygments`` and ``pygmentize``,
    but ``pypi2pkgbuild.py`` only sees the former (and thus does not
    provides/conflicts the latter).  ``pkgbase`` could be read out of
    ``.PKGINFO``, but does pacman provide a way to find packages given a
    ``pkgbase``?

  - Arch splits ``pygobject`` into ``python-gobject`` and ``pygobject-devel``,
    as the latter shares a header with ``python2-gobject``.  Trying
    to build our own ``python-gobject`` (after manually installing
    ``gobject-instrospection`` and declaring a setup_requires on ``pycairo``,
    see above) results in a collision on that header.

- Packages that install system-level (e.g., systemd) scripts:

  - ``sftpman``.

- Other build failures:

  - ``gr``.

Note that fixes for some other packages are provided in the ``pkgbuild-extras``
directory.

Ideas for extracting makedepends
================================

- Intercept pkg-config calls for missing packages (e.g. cairo/pycairo?).
- Extract manylinux wheels' vendored .libs.
