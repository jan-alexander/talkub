"""talkub - admin CLI for Talos cluster repos."""

from importlib.metadata import version

# The version lives only in pyproject.toml; the Nuitka binary ships the distribution metadata.
__version__ = version("talkub")
