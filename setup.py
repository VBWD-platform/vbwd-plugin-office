"""Wheel package list: this repo root as ``plugins.office``, plus its discovered sub-packages.

``pyproject.toml`` holds the static metadata; only the prefixed package discovery lives here.
``script_name`` names this file (relative to the project dir, the build cwd) so setuptools
leaves it out of the root package's modules — under PEP 517 the default, ``sys.argv[0]``,
is the build-backend hook, not this file.
"""
from pathlib import Path

from setuptools import find_packages, setup

PACKAGE_ROOT = "plugins.office"

setup(
    script_name=Path(__file__).name,
    packages=[PACKAGE_ROOT]
    + [
        f"{PACKAGE_ROOT}.{package}"
        for package in find_packages(exclude=["tests", "tests.*"])
    ],
)
