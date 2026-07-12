"""Shared pytest configuration.

pytest-textual-snapshot 1.0.0 sets ``SVGImageExtension._file_extension``
(an older syrupy attribute name); the installed syrupy release reads
``file_extension`` instead, so without this patch baseline snapshots would
be written with a generic ``.raw`` extension rather than ``.svg``.
"""

from pytest_textual_snapshot import SVGImageExtension

SVGImageExtension.file_extension = "svg"
