"""Typed configuration and hardware profiles."""

from .profiles import PROFILES, ModelProfile, ProfileName, get_profile, recommend_profile
from .settings import PROJECT_ROOT, Settings, get_settings, reset_settings_cache

__all__ = [
    "PROFILES", "ModelProfile", "ProfileName", "get_profile", "recommend_profile",
    "PROJECT_ROOT", "Settings", "get_settings", "reset_settings_cache",
]
