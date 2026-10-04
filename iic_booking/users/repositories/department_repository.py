"""Repository for Department model."""

from typing import Optional

from django.db.models import QuerySet

from ..models import Department
from ..models.department import DepartmentType, InternalDepartmentSubcategory


class DepartmentRepository:
    """Repository for Department operations."""

    @staticmethod
    def get_all() -> QuerySet[Department]:
        """Get all departments ordered by name."""
        return Department.objects.all().order_by("name")

    @staticmethod
    def get_iitr_departments_and_centres() -> QuerySet[Department]:
        """IIT Roorkee departments and centres offered to IITR Post-docs, Research Associates and IITR Startup.

        Every internal department counts unless it is a Startups entry or the ADMIN department; most
        internal departments have no subcategory set, so the subcategory is not required.
        """
        return (
            Department.objects.filter(department_type=DepartmentType.INTERNAL)
            .exclude(internal_subcategory=InternalDepartmentSubcategory.STARTUPS)
            .exclude(name__iexact="ADMIN")
            .exclude(code__iexact="ADMIN")
            .order_by("name")
        )

    @staticmethod
    def get_by_id(pk: int) -> Optional[Department]:
        """Get department by primary key."""
        try:
            return Department.objects.get(pk=pk)
        except Department.DoesNotExist:
            return None

    @staticmethod
    def get_by_code(code: str) -> Optional[Department]:
        """Get department by code."""
        try:
            return Department.objects.get(code=code)
        except Department.DoesNotExist:
            return None

    @staticmethod
    def create(**kwargs) -> Department:
        """Create a new department."""
        return Department.objects.create(**kwargs)

    @staticmethod
    def update(department: Department, **kwargs) -> Department:
        """Update department fields."""
        for key, value in kwargs.items():
            setattr(department, key, value)
        department.save()
        return department

    @staticmethod
    def delete(department: Department) -> None:
        """Delete a department."""
        department.delete()

