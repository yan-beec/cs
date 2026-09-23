"""Disable Chroma product telemetry for local and self-hosted deployments."""

from __future__ import annotations

from typing import Any

from chromadb.telemetry.product import ProductTelemetryClient, ProductTelemetryEvent
from overrides import override


class DisabledProductTelemetry(ProductTelemetryClient):
    """A no-op Chroma telemetry implementation."""

    @override
    def capture(self, event: ProductTelemetryEvent) -> None:
        return None


def build_chroma_settings(settings_type: Any) -> Any:
    """Build Chroma settings with telemetry disabled at the implementation level."""
    return settings_type(
        anonymized_telemetry=False,
        chroma_product_telemetry_impl=(
            "knowledge_base_in.chroma_telemetry.DisabledProductTelemetry"
        ),
    )
