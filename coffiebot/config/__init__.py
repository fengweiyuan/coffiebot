"""Configuration module for coffiebot."""

from coffiebot.config.loader import load_config, get_config_path
from coffiebot.config.schema import Config

__all__ = ["Config", "load_config", "get_config_path"]
