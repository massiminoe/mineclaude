"""Root-level pytest config.

Adds --run-e2e for opting in to the docker-compose end-to-end suite. E2E
tests are skipped by default so unit test runs stay fast.
"""

from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-e2e",
        action="store_true",
        default=False,
        help="Run end-to-end tests that require docker-compose (slow).",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if config.getoption("--run-e2e"):
        return
    skip_e2e = pytest.mark.skip(reason="e2e test (pass --run-e2e to enable)")
    for item in items:
        if "e2e" in item.keywords:
            item.add_marker(skip_e2e)
