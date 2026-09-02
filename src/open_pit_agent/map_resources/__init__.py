"""Persistent, map-scoped spatial resources for open-pit scenarios.

This package intentionally contains no scheduling, CARLA control, or risk
decision logic.  It is the long-lived spatial fact layer from which a later
scenario generator can select verified points and routes.
"""

from .store import MapResourceStore

__all__ = ["MapResourceStore"]
