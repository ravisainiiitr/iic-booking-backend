"""Serializers for Project model."""

from rest_framework import serializers

from ..models import Project


class ProjectSerializer(serializers.ModelSerializer[Project]):
    """Serializer for Project model."""

    faculty = serializers.IntegerField(source="faculty.id", read_only=True)
    is_expired = serializers.BooleanField(read_only=True)

    class Meta:
        model = Project
        fields = [
            "id",
            "name",
            "project_code",
            "agency",
            "start_date",
            "end_date",
            "is_active",
            "is_expired",
            "faculty",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "faculty", "created_at", "updated_at", "is_expired"]


class ProjectCreateSerializer(serializers.Serializer):
    """Serializer for creating a new project."""

    name = serializers.CharField(
        max_length=255,
        help_text="Name of the research project",
    )
    project_code = serializers.CharField(
        max_length=100,
        help_text="Unique code for the project",
    )
    agency = serializers.CharField(
        max_length=255,
        help_text="Name of the funding agency",
    )
    start_date = serializers.DateField(
        required=False,
        allow_null=True,
        help_text="Project start date",
    )
    end_date = serializers.DateField(
        required=False,
        allow_null=True,
        help_text="Project end date",
    )

    def validate(self, data):
        """Validate that end_date is after start_date if both are provided."""
        start_date = data.get("start_date")
        end_date = data.get("end_date")
        
        if start_date and end_date and end_date < start_date:
            raise serializers.ValidationError(
                "End date must be after start date."
            )
        
        return data


class ProjectUpdateSerializer(serializers.Serializer):
    """Serializer for updating a project."""

    name = serializers.CharField(
        max_length=255,
        required=False,
        help_text="Name of the research project",
    )
    project_code = serializers.CharField(
        max_length=100,
        required=False,
        help_text="Unique code for the project",
    )
    agency = serializers.CharField(
        max_length=255,
        required=False,
        help_text="Name of the funding agency",
    )
    start_date = serializers.DateField(
        required=False,
        allow_null=True,
        help_text="Project start date",
    )
    end_date = serializers.DateField(
        required=False,
        allow_null=True,
        help_text="Project end date",
    )
    is_active = serializers.BooleanField(
        required=False,
        help_text="Whether the project is active",
    )

    def validate(self, data):
        """Validate that end_date is after start_date if both are provided."""
        start_date = data.get("start_date")
        end_date = data.get("end_date")
        
        # If updating, get existing values from instance if not provided
        # self.instance is available when serializer is initialized with instance parameter
        if hasattr(self, 'instance') and self.instance is not None:
            start_date = start_date if start_date is not None else self.instance.start_date
            end_date = end_date if end_date is not None else self.instance.end_date
        
        if start_date and end_date and end_date < start_date:
            raise serializers.ValidationError(
                "End date must be after start date."
            )
        
        return data
