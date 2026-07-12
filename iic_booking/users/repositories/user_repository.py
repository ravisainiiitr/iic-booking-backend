"""Repository for User model."""

from typing import Optional

from django.db.models import QuerySet

from ..models import User, UserType


class UserRepository:
    """Repository for User operations."""

    @staticmethod
    def get_all() -> QuerySet[User]:
        """Get all users."""
        return User.objects.all()

    @staticmethod
    def get_by_id(pk: int) -> Optional[User]:
        """Get user by primary key."""
        try:
            return User.objects.get(pk=pk)
        except User.DoesNotExist:
            return None

    @staticmethod
    def get_by_email(email: str) -> Optional[User]:
        """Get user by email."""
        try:
            return User.objects.get(email=email)
        except User.DoesNotExist:
            return None

    @staticmethod
    def get_by_user_type(user_type_code: str) -> QuerySet[User]:
        """Get users by user type code.
        
        Args:
            user_type_code: User type code (e.g., 'student', 'faculty')
            
        Returns:
            QuerySet of User objects
        """
        return User.objects.filter(user_type=user_type_code).select_related("department")

    @staticmethod
    def get_faculty() -> QuerySet[User]:
        """Get all faculty members."""
        return UserRepository.get_by_user_type(UserType.FACULTY)

    @staticmethod
    def get_students() -> QuerySet[User]:
        """Get all students."""
        return UserRepository.get_by_user_type(UserType.STUDENT)

    @staticmethod
    def create(**kwargs) -> User:
        """Create a new user."""
        return User.objects.create_user(**kwargs)

    @staticmethod
    def update(user: User, **kwargs) -> User:
        """Update user fields."""
        for key, value in kwargs.items():
            setattr(user, key, value)
        user.save()
        return user

    @staticmethod
    def delete(user: User) -> None:
        """Delete a user."""
        user.delete()

