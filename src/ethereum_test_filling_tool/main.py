"""
Ethereum Test Filler
^^^^^^^^^^^^^^^^^^^^

Execute test fillers to create "filled" tests that can be consumed by execution
clients.
"""

import argparse
import concurrent.futures
import json
import logging
import os
import time
from pathlib import Path
from pkgutil import iter_modules

from setuptools import find_packages

from ethereum_test_tools import JSONEncoder
from evm_block_builder import EvmBlockBuilder
from evm_transition_tool import EvmTransitionTool


class Filler:
    """
    A command line tool to process test fillers into full hydrated tests.
    """

    @staticmethod
    def parse_arguments() -> argparse.Namespace:
        """
        Parse command line arguments.
        """
        parser = argparse.ArgumentParser()

        parser.add_argument(
            "--evm-bin",
            help="path to evm executable that provides `t8n` and `b11r` \
                  subcommands",
            default=None,
            type=Path,
        )

        parser.add_argument(
            "--filler-path",
            help="path to filler directives, default: ./fillers",
            default="fillers",
            type=Path,
        )

        parser.add_argument(
            "--output",
            help="directory to store filled test fixtures, \
                  default: ./fixtures",
            default="fixtures",
            type=Path,
        )

        parser.add_argument(
            "--test-categories",
            type=str,
            nargs="+",
            help="limit to filling tests of specific categories",
        )

        parser.add_argument(
            "--test-module",
            help="limit to filling tests of a specific module",
        )

        parser.add_argument(
            "--test-case",
            help="limit to filling only tests with matching name",
        )

        parser.add_argument(
            "--traces",
            action="store_true",
            help="collect traces of the execution information from the "
            + "transition tool",
        )

        parser.add_argument(
            "--no-output-structure",
            action="store_true",
            help="removes the folder structure from test fixture output",
        )

        parser.add_argument(
            "--benchmark",
            action="store_true",
            help="logs the timing of the test filler for benchmarking",
        )

        parser.add_argument(
            "--max-workers",
            type=int,
            help="specifies the max number of workers for the test filler \
                  set to 1 for serial execution",
        )

        parser.add_argument(
            "--no-skip",
            action="store_true",
            help="fill all test fillers and don't skip any tests",
        )

        return parser.parse_args()

    options: argparse.Namespace
    log: logging.Logger

    def __init__(self) -> None:
        self.log = logging.getLogger(__name__)
        self.options = self.parse_arguments()

    def fill(self) -> None:
        """
        Fill test fixtures.
        """
        if self.options.benchmark:
            start_time = time.time()

        pkg_path = self.options.filler_path

        fillers = []

        for package_name, module_name, module_loader in find_modules(
            os.path.abspath(pkg_path),
            self.options.test_categories,
            self.options.test_module,
        ):
            module_full_name = module_loader.name
            self.log.debug(f"searching {module_full_name} for fillers")
            module = module_loader.load_module()
            for obj in module.__dict__.values():
                if callable(obj):
                    if hasattr(obj, "__filler_metadata__"):
                        if (
                            self.options.test_case
                            and self.options.test_case
                            not in obj.__filler_metadata__["name"]
                        ):
                            continue
                        obj.__filler_metadata__["module_path"] = [
                            package_name,
                            module_name,
                        ]
                        fillers.append(obj)

        self.log.info(f"collected {len(fillers)} fillers")

        os.makedirs(self.options.output, exist_ok=True)

        t8n = EvmTransitionTool(
            binary=self.options.evm_bin, trace=self.options.traces
        )
        b11r = EvmBlockBuilder(binary=self.options.evm_bin)

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=self.options.max_workers
        ) as executor:
            futures = []
            for filler in fillers:
                name = filler.__filler_metadata__["name"]
                module_path = filler.__filler_metadata__["module_path"]
                output_dir = os.path.join(
                    self.options.output,
                    *module_path
                    if not self.options.no_output_structure
                    else "",
                )
                os.makedirs(output_dir, exist_ok=True)
                path = os.path.join(output_dir, f"{name}.json")
                full_name = ".".join(module_path + [name])

                if (
                    skip_filling(path, pkg_path, module_path)
                    and not self.options.no_skip
                ):
                    self.log.debug(f"skipping - {full_name}")
                    continue

                future = executor.submit(filler, t8n, b11r, "NoProof")
                futures.append((future, path, full_name))

            for future, path, full_name in futures:
                fixture = future.result()
                self.log.debug(f"filling - {full_name}")
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(
                        fixture,
                        f,
                        ensure_ascii=False,
                        indent=4,
                        cls=JSONEncoder,
                    )

            if self.options.benchmark:
                end_time = time.time()
                elapsed_time = end_time - start_time
                self.log.info(
                    f"Filled test fixtures in {elapsed_time:.2f} seconds."
                )


def skip_filling(path, pkg_path, module_path):
    last_modified_time = os.path.getmtime(
        os.path.join(pkg_path, *module_path) + ".py"
    )
    last_filled_time = os.path.getmtime(path) if os.path.exists(path) else 0
    return last_modified_time <= last_filled_time


def find_modules(root, include_pkg, include_modules):
    """
    Find modules recursively starting with the `root`.
    Only modules in a package with name found in iterable `include_pkg` will be
    yielded.
    Only modules with name found in iterable `include_modules` will be yielded.
    """
    modules = set()
    for package in find_packages(
        root,
        include=include_pkg if include_pkg is not None else ("*",),
    ):
        package = package.replace(
            ".", "/"
        )  # sub_package tests i.e 'vm.vm_tests'
        for info, package_path in recursive_iter_modules(root, package):
            module_full_name = package_path + "." + info.name
            if module_full_name not in modules:
                if not include_modules or include_modules in info.name:
                    yield (
                        package,
                        info.name,
                        info.module_finder.find_module(module_full_name),
                    )
                modules.add(module_full_name)


def recursive_iter_modules(root, package):
    """
    Helper function for find_packages.
    Iterates through all sub-packages (packages within a package).
    Recursively navigates down the package tree until a new module is found.
    """
    for info in iter_modules([os.path.join(root, package)]):
        if info.ispkg:
            yield from recursive_iter_modules(
                root, os.path.join(package, info.name)
            )
        else:
            package_path = package.replace("/", ".")
            yield info, package_path


def main() -> None:
    """
    Fills the specified test definitions.
    """
    logging.basicConfig(level=logging.DEBUG)

    filler = Filler()
    filler.fill()
