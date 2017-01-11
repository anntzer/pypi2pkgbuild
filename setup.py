from setuptools import setup


if __name__ == "__main__":
    setup(name="pypi2pkgbuild",
          version="0.1",
          description="A PyPI to PKGBUILD converter.",
          url="https://github.com/anntzer/pypi2pkgbuild",
          author="Antony Lee",
          license="BSD",
          scripts=["pypi2pkgbuild.py"],
          classifiers=["Development Status :: 4 - Beta",
                       "Environment :: Console",
                       "Intended Audience :: System Administrators",
                       "License :: OSI Approved :: BSD License",
                       "Operating System :: POSIX :: Linux",
                       "Programming Language :: Python :: 3.5",
                       "Topic :: System :: Software Distribution"],
          install_requires=["pip>=9"])
