"""Loopback bridge that lets PotPlayer browse and stream Quark Drive files.

The package has no video cache: it proxies byte ranges from Quark on demand and
stores only credentials, runtime configuration, and in-memory play sessions.
"""

SERVICE_VERSION = "1"

__all__ = ["SERVICE_VERSION"]
