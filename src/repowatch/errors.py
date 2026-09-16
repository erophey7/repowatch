"""Shared configuration and integrity errors."""

from __future__ import annotations


class ConfigError(Exception):
    """Invalid configuration."""


class SignatureError(Exception):
    """The signature is missing, invalid, expired, or the required public
    key isn't in the keyring. The caller must not trust the data."""
