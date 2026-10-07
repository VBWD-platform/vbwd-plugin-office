"""The built wheel ships every file this plugin loads at runtime, and nothing from tests/.

Same check as vbwd-plugin-theme ``tests/unit/test_wheel_packaging.py`` (its canonical copy);
the package root (``plugins.<name>``) is read from this repo's ``pyproject.toml``.

The repo is copied to a temp dir (so no build artefacts land in the checkout), built with
``pip wheel --no-build-isolation`` (offline: needs setuptools + wheel in the environment),
installed with ``pip install --target``, then compared file-by-file with the source tree.
"""
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import plugins

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = Path(plugins.__file__).resolve().parent.parent
NON_RUNTIME_TOP_LEVEL_DIRECTORIES = frozenset({"tests", "bin", "docs"})
BUILD_ONLY_FILES = frozenset({"setup.py"})
RUNTIME_TOP_LEVEL_SUFFIXES = frozenset({".py", ".json"})
IGNORED_DIRECTORY_NAMES = frozenset({"__pycache__", "build", "dist"})
IGNORED_FILE_SUFFIXES = frozenset({".pyc", ".pyo"})


def _package_root() -> str:
    with open(REPO_ROOT / "pyproject.toml", "rb") as pyproject_file:
        package_directories = tomllib.load(pyproject_file)["tool"]["setuptools"][
            "package-dir"
        ]
    (package_root,) = package_directories
    return package_root


PACKAGE_ROOT = _package_root()


def _is_ignored(relative_path: Path) -> bool:
    for part in relative_path.parts:
        if (
            part.startswith(".")
            or part in IGNORED_DIRECTORY_NAMES
            or part.endswith(".egg-info")
        ):
            return True
    return relative_path.suffix in IGNORED_FILE_SUFFIXES


def _is_runtime_file(relative_path: Path) -> bool:
    if len(relative_path.parts) == 1:
        return (
            relative_path.suffix in RUNTIME_TOP_LEVEL_SUFFIXES
            and relative_path.name not in BUILD_ONLY_FILES
        )
    return relative_path.parts[0] not in NON_RUNTIME_TOP_LEVEL_DIRECTORIES


def _runtime_source_files() -> set:
    return {
        relative_path
        for relative_path in (
            path.relative_to(REPO_ROOT)
            for path in REPO_ROOT.rglob("*")
            if path.is_file()
        )
        if not _is_ignored(relative_path) and _is_runtime_file(relative_path)
    }


def _run_pip(*arguments: str) -> None:
    subprocess.run([sys.executable, "-m", "pip", *arguments, "--quiet"], check=True)


@pytest.fixture(scope="module")
def installed_package_dir(tmp_path_factory) -> Path:
    work_dir = tmp_path_factory.mktemp("wheel_packaging")
    source_copy = work_dir / "source"
    shutil.copytree(
        REPO_ROOT,
        source_copy,
        symlinks=True,
        ignore=shutil.ignore_patterns(
            ".git", "__pycache__", "build", "dist", "*.egg-info"
        ),
    )
    wheel_dir = work_dir / "wheels"
    _run_pip(
        "wheel",
        "--no-deps",
        "--no-build-isolation",
        "--no-index",
        "--wheel-dir",
        str(wheel_dir),
        str(source_copy),
    )
    (wheel_file,) = wheel_dir.glob("*.whl")
    install_target = work_dir / "target"
    _run_pip(
        "install",
        "--no-deps",
        "--no-index",
        "--target",
        str(install_target),
        str(wheel_file),
    )
    return install_target.joinpath(*PACKAGE_ROOT.split("."))


def _installed_files(installed_package_dir: Path) -> set:
    return {
        path.relative_to(installed_package_dir)
        for path in installed_package_dir.rglob("*")
        if path.is_file() and not _is_ignored(path.relative_to(installed_package_dir))
    }


def test_the_source_tree_has_runtime_files_to_check():
    assert Path("__init__.py") in _runtime_source_files()


def test_the_wheel_ships_every_runtime_file(installed_package_dir):
    missing_files = sorted(
        str(path)
        for path in _runtime_source_files() - _installed_files(installed_package_dir)
    )
    assert (
        not missing_files
    ), f"{len(missing_files)} runtime file(s) missing from the wheel: {missing_files}"


def test_the_wheel_ships_neither_tests_nor_build_only_files(installed_package_dir):
    assert not (installed_package_dir / "tests").exists()
    for build_only_file in BUILD_ONLY_FILES:
        assert not (
            installed_package_dir / build_only_file
        ).exists(), f"{build_only_file} shipped"


def test_the_installed_package_root_imports(installed_package_dir):
    import_script = (
        "import importlib, sys, plugins\n"
        f"plugins.__path__.insert(0, {str(installed_package_dir.parent)!r})\n"
        f"print(importlib.import_module({PACKAGE_ROOT!r}).__file__)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", import_script],
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert Path(completed.stdout.strip()).is_relative_to(installed_package_dir)
