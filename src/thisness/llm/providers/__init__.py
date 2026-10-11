"""Model providers. Each adapter imports its SDK only when constructed, so
``thisness`` installs and runs without any of them (PRD appendix A.2)."""

from thisness.llm.providers.function import FunctionProvider
from thisness.llm.providers.replay import RecordingProvider, ReplayProvider, request_key

__all__ = ["FunctionProvider", "RecordingProvider", "ReplayProvider", "request_key"]
