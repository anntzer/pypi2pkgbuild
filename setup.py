from setuptools import setup
import versioneer


if __name__ == "__main__":
    setup(
        name="pypi2pkgbuild",
        version=versioneer.get_version(),
        cmdclass=versioneer.get_cmdclass(),
        description="A PyPI to PKGBUILD converter.",
        url="https://github.com/anntzer/pypi2pkgbuild",
        author="Antony Lee",
        license="BSD",
        scripts=["pypi2pkgbuild.py"],
        classifiers=[
            "Development Status :: 4 - Beta",
            "Environment :: Console",
            "Intended Audience :: System Administrators",
            "License :: OSI Approved :: BSD License",
            "Operating System :: POSIX :: Linux",
            "Programming Language :: Python :: 3.5",
            "Programming Language :: Python :: 3.6",
            "Topic :: System :: Software Distribution",
        ],
        python_requires=">=3.5",
        install_requires=["pip>=9"],
    )
