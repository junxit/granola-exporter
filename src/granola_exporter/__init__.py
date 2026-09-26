"""Local, incremental archive of Granola meetings, transcripts and summaries."""

from importlib.metadata import PackageNotFoundError, version

try:
    # pyproject.toml is the one place the version is written. A copy kept
    # here by hand sat at 0.3.0 through two releases, so every request in
    # between announced the wrong version to Granola.
    __version__ = version("granola-exporter")
except PackageNotFoundError:  # a source tree that was never installed
    __version__ = "0+unknown"

# Sent as the User-Agent by both backends. Kept here so there is one source
# of truth: it was previously hardcoded a third time in public_api.py, which
# is exactly the copy that goes stale.
USER_AGENT = f"granola-exporter/{__version__}"
