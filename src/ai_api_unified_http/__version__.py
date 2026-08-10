# src/ai_api_unified_http/__version__.py

"""
Single source of the service version at runtime.

Keep in sync with pyproject.toml and the README.md title. The mocked test
suite (tests/test_version_sync.py) fails whenever the three disagree.
"""

__all__: list[str] = ["__version__"]

__version__: str = "1.5.0"
