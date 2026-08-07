"""Local, incremental archive of Granola meetings, transcripts and summaries."""

__version__ = "0.2.0"

# Sent as the User-Agent by both backends. Kept here so there is one source
# of truth: it was previously hardcoded a third time in public_api.py, which
# is exactly the copy that goes stale.
USER_AGENT = f"granola-exporter/{__version__}"
