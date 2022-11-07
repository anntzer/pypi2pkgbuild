from setuptools import setup


setup(
    name="pypi2pkgbuild",
    description="A PyPI to PKGBUILD converter.",
    long_description=open("README.rst", encoding="utf-8").read(),
    url="https://github.com/anntzer/pypi2pkgbuild",
    author="Antony Lee",
    license="MIT",
    classifiers=[
        "Development Status :: 4 - Beta",
        "Environment :: Console",
        "Intended Audience :: System Administrators",
        "License :: OSI Approved :: MIT License",
        "Operating System :: POSIX :: Linux",
        "Programming Language :: Python :: 3",
        "Topic :: System :: Software Distribution",
    ],
    package_dir={"": "lib"},
    python_requires=">=3.8",
    setup_requires=["setuptools_scm"],
    use_scm_version=lambda: {  # xref pypi2pkgbuild.py
        "version_scheme": "post-release",
        "local_scheme": "node-and-date",
    },
    scripts=["pypi2pkgbuild.py"],
)
