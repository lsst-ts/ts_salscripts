import os
import sys
import setuptools
from setuptools import setup, find_namespace_packages
import pathlib

install_requires = ["astropy", "astroquery"]
tests_require = ["pytest", "pytest-cov", "pytest-flake8"]
dev_requires = install_requires + tests_require + ["documenteer[pipelines]"]
tools_path = pathlib.PurePosixPath(setuptools.__path__[0])
base_prefix = pathlib.PurePosixPath(sys.base_prefix)
data_files_path = tools_path.relative_to(base_prefix).parents[1]
data_files = []

for dirpath, dirnames, filenames in os.walk(
    "scripts/", topdown=True, followlinks=False
):
    dirnames[:] = [name for name in dirnames if not name.startswith(".")]
    destination = os.path.join(data_files_path, dirpath)
    script_files = [
        os.path.join(dirpath, filename)
        for filename in filenames
        if filename[0] not in (".", "_")
    ]
    data_files.append((destination, script_files))

scm_version_template = """# Generated by setuptools_scm
__all__ = ["__version__"]

__version__ = "{version}"
"""

setup(
    name="ts_externalscripts",
    description="External SAL scripts for LSST observing with the script queue.",
    use_scm_version={
        "write_to": "python/lsst/ts/externalscripts/version.py",
        "write_to_template": scm_version_template,
    },
    data_files=data_files,
    include_package_data=True,
    setup_requires=["setuptools_scm", "pytest-runner"],
    install_requires=install_requires,
    package_dir={"": "python"},
    packages=find_namespace_packages(where="python"),
    package_data={"": ["*.rst", "*.yaml"]},
    tests_require=tests_require,
    extras_require={"dev": dev_requires},
    license="GPL",
    project_urls={
        "Bug Tracker": "https://jira.lsstcorp.org/secure/Dashboard.jspa",
        "Source Code": "https://github.com/lsst-ts/ts_externalscripts",
    },
)
