- mistune has an any wheel but an optional arch dependency.  Need to repackage
  after namcap complains / build the wheel in a venv / whatever...

Failing builds
==============

extra_requires
--------------

- scikit-image: depends on dask(array)

licensing
---------

- profilehooks: need scraping to find link to github (MIT)
- versioneer: no classifier (public domain)

others
------

- gatspy: does not declare dependencies
- hmmlearn: does not declare dependencies
- nitime: does not declare dependencies
- yep: depends on gperftools
