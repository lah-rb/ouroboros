"""Data models for the project.

This module defines Pydantic v2 data models that can be used throughout the
application for validation, serialization and type‑checking.

The models are deliberately simple and generic so they can serve as a
starting point for the rest of the codebase.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, EmailStr, Field


class User(BaseModel):
    """Representation of an application user."""

    id: int = Field(..., description="Unique identifier of the user.")
    username: str = Field(..., min_length=3, max_length=30, description="Login name.")
    email: EmailStr = Field(..., description="User's e‑mail address.")
    is_active: bool = Field(default=True, description="Indicates if the user is active.")
    full_name: Optional[str] = Field(
        default=None,
        description="Optional full name of the user.",
    )

    model_config = {
        "json_schema_extra": {"example": {"id": 1, "username": "alice", "email": "alice@example.com"}},
    }


class Item(BaseModel):
    """A generic item owned by a user."""

    id: int = Field(..., description="Unique identifier of the item.")
    owner_id: int = Field(..., description="Identifier of the owning user.")
    name: str = Field(..., min_length=1, description="Human‑readable name of the item.")
    description: Optional[str] = Field(default=None, description="Optional detailed description.")
    tags: List[str] = Field(default_factory=list, description="List of tags associated with the item.")
    price_cents: int = Field(..., ge=0, description="Price in cents; non‑negative integer.")

    model_config = {
        "json_schema_extra": {"example": {"id": 10, "owner_id": 1, "name": "Widget", "price_cents": 1999}},
    }


class Settings(BaseModel):
    """Application configuration settings."""

    debug: bool = Field(default=False, description="Enable debug mode.")
    allowed_hosts: List[str] = Field(
        default_factory=lambda: ["*"],
        description="List of hostnames/IPs the app may serve.",
    )
    secret_key: str = Field(..., min_length=32, description="Secret key for cryptographic signing.")

    model_config = {
        "json_schema_extra": {"example": {"debug": True, "allowed_hosts": ["localhost"], "secret_key": "a"*32}},
    }


__all__ = ["User", "Item", "Settings"]
