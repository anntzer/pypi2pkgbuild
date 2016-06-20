Features
========

- Update depends based on namcap's dependency-detected-not-included.

Failing builds
==============

extra_requires
--------------

- scikit-image: depends on dask(array)

licensing
---------

- bibtex-pygments-lexer: no license info except on github
- dill: no classifier, license in sdist (3-BSD)
- hmmlearn: invalid classifier (BSD)
- nbstripout: classifier used as license (MIT)
- profilehooks: need scraping to find link to github (MIT)
- versioneer: no classifier (public domain)

others
------

- jupyter_qtconsole_colorschemes: download from bitbucket (.../raw/master/...) (also, parse URLs for both github and bitbucket)
- statprof-smarkets: confusing tgz from bdist_dumb
- pygments-markdown-lexer: bad wheel (data files)
- nitime: does not declare dependencies
