"""Expose root statistics/ as ics_agent.statistics without shadowing stdlib statistics."""

from pathlib import Path

__path__ = [str(Path(__file__).resolve().parents[1] / "statistics")]
