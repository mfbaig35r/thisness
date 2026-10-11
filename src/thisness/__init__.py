"""Thisness: identity resolution for imperfect observations."""

from importlib.metadata import PackageNotFoundError, version

from thisness.engine import ObservationOutcome, RunResult, Thisness
from thisness.ids import IdFactory

try:
    __version__ = version("thisness")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0"

__all__ = ["IdFactory", "ObservationOutcome", "RunResult", "Thisness", "__version__"]
