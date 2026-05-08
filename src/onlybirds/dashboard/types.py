"""Shared dashboard types — kept tiny to avoid import cycles."""

from enum import Enum


class HotspotKind(str, Enum):
    CONSOLIDATED = "consolidated"
    HOTSPOT = "hotspot"
