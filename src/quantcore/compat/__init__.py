"""Compatibility boundaries for historical evidence.

The active application imports only the local archive adapter.  External
source readers remain isolated in explicitly offline migration tools.
"""

from .local_archive import LocalArchiveAdapter

__all__ = ["LocalArchiveAdapter"]
