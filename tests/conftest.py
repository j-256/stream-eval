"""Shared pytest configuration."""
import pytest


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "integration: tests that spawn real `claude -p` processes",
    )
