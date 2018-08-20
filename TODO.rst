Issues
======

- Due to pypa/setuptools#5429 (devendoring issues with pip), combining an
  Arch-packaged pip and a ``pypi2pkgbuild.py``-packaged setuptools does not
  work.  In practice, this means that one should exclude ``setuptools`` from
  automatic upgrades.

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
  **NOTE:** This may possibly be fixed using the ``NPY_DISTUTILS_APPEND_FLAG``
  environment variable on numpy≥1.16.

- ``fpm`` adds a ``get_metadata`` command to avoid having to install the
  package but this can't be done with e.g. wheels.  Perhaps we could hook
  something else?

- Move ``numpy`` support to ``--guess-makedepends``.  Implement
  ``guess-makedepends`` by adding shim files to the environment that check
  whether they are accessed.  A similar strategy can be used e.g. for swig,
  pybind11.

- Investigate placement of ``/etc`` under ``/share`` or not
  (``widgetsnbextension`` does it right, not ``plotly``, but it's unclear why).
  Also consider auto-moving this directory to the right place; other similar
  cases: ``tqdm`` (currently special-cased).

Arch packaging
==============

- Some packages are installed without an ``.egg-info`` (e.g. ``entrypoints``)
   and thus not seen by ``pip list --outdated`` (and thus
  ``pypi2pkgbuild.py -o``).

Other mispackaged packages
==========================

- Setup-time non-Python dependencies.

  - ``notebook`` *from the git repository* (requires at least ``bower``,
    perhaps more).

- Undetected split packages:

  - Arch splits ``pygments`` into ``python-pygments`` and ``pygmentize``,
    and ``pygobject`` into ``python-gobject`` and ``pygobject-devel``
    (because the latter is shared with ``python2-gobject``) but in each
    case ``pypi2pkgbuild.py`` only sees the former (and thus does not
    provides/conflicts the latter).  Due to licenses and the presence of
    custom scripts (e.g. shell completion for ``pygmentize``, we can't rely
    on strict inclusion).  The best solution is therefore either to declare a
    ``conflicts``/``replaces``, or to manually remove the extraneous file (see
    ``python-gobject.PKGBUILD_EXTRAS``).

- Packages that install system-level (e.g., systemd) scripts:

  - ``sftpman`` (explicitly unsupported via ``sftpman.PKGBUILD_EXTRAS``).

- Packages vendored into non-Python packages (could be partially detected from
  nonmatching versions):

  - ``lit`` (vendored into ``llvm``).

- "Weird" ``setup.py``\s:

  - ``pytest-pycodestyle`` (``setup.py`` packages both ``pytest-pycodestyle``
    and ``pytest-codestyle``).

Note that fixes for some other packages are provided in the ``pkgbuild-extras``
directory.

Ideas for extracting makedepends
================================

- Intercept pkg-config calls for missing packages (e.g. cairo/pycairo?).
- Extract manylinux wheels' vendored .libs.
