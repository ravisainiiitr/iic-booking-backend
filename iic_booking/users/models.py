"""Backward compatibility: Import all models from the models package."""

# Import all models from the new models package structure
from .models import (
    UserManager,
    Department,
    User,
    UserType,
)

__all__ = [
    "UserManager",
    "Department",
    "User",
    "UserType",
]
