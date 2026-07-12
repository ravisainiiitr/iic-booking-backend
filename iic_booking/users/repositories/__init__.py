"""Repositories package for users app."""

from .department_repository import DepartmentRepository
from .user_repository import UserRepository

__all__ = [
    "DepartmentRepository",
    "UserRepository",
]

