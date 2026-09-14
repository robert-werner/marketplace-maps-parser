"""marketplace-maps-parser — async review scraper for Russian marketplaces.

This package wires the domain, application, and infrastructure layers together
and exposes a single CLI entrypoint (``python -m marketplace_maps_parser``).

The legacy ``main.py`` / ``main_2.py`` / ``main_3.py`` / ``main_4.py`` scripts
at the repository root are kept only for backward reference; new code should
import from this package or use the CLI.
"""
from __future__ import annotations

__version__ = "0.1.0"
