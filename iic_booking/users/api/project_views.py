"""API views for project management."""

from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status

from ..models import Project, User
from ..models.user_type import UserType
from ..serializers.project_serializer import (
    ProjectSerializer,
    ProjectCreateSerializer,
    ProjectUpdateSerializer,
)


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def project_list(request):
    """Get list of projects or create a new project.
    
    GET: Returns list of projects for the current user
    POST: Creates a new project for the current user
    
    Request Body (POST):
        - name: Project name (required)
        - project_code: Project code (required)
        - agency: Funding agency (required)
        - start_date: Start date (optional)
        - end_date: End date (optional)
    
    Returns:
        GET: {
            - projects: List of projects belonging to the current user
            - count: Total number of projects
        }
        POST: Created project object
    """
    # Only faculty can have projects
    if request.user.user_type != UserType.FACULTY:
        return Response(
            {"error": "Only faculty members can access projects."},
            status=status.HTTP_403_FORBIDDEN,
        )
    
    if request.method == "GET":
        projects = Project.objects.filter(faculty=request.user)
        
        # Auto-disable expired projects
        from django.utils import timezone
        today = timezone.localdate()
        expired_projects = projects.filter(end_date__lt=today, is_active=True)
        if expired_projects.exists():
            expired_projects.update(is_active=False)
            # Refresh queryset
            projects = Project.objects.filter(faculty=request.user)
        
        serializer = ProjectSerializer(projects, many=True)
        return Response({
            "projects": serializer.data,
            "count": len(serializer.data),
        })
    
    elif request.method == "POST":
        serializer = ProjectCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        # Create project with current user as faculty
        project = Project.objects.create(
            faculty=request.user,
            name=serializer.validated_data["name"],
            project_code=serializer.validated_data["project_code"],
            agency=serializer.validated_data["agency"],
            start_date=serializer.validated_data.get("start_date"),
            end_date=serializer.validated_data.get("end_date"),
        )
        
        response_serializer = ProjectSerializer(project)
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)


@api_view(["GET", "PATCH", "PUT"])
@permission_classes([IsAuthenticated])
def project_detail(request, project_id):
    """Get, update, or partially update a project.
    
    GET: Returns project details
    PATCH/PUT: Updates project fields
    
    Args:
        project_id: ID of the project
    
    Request Body (PATCH/PUT):
        - name: Project name (optional)
        - project_code: Project code (optional)
        - agency: Funding agency (optional)
        - start_date: Start date (optional)
        - end_date: End date (optional)
        - is_active: Whether project is active (optional)
    
    Returns:
        GET: Project object
        PATCH/PUT: Updated project object
    """
    # Only faculty can access projects
    if request.user.user_type != UserType.FACULTY:
        return Response(
            {"error": "Only faculty members can access projects."},
            status=status.HTTP_403_FORBIDDEN,
        )
    
    # Get project and ensure it belongs to the current user
    project = get_object_or_404(Project, id=project_id, faculty=request.user)
    
    if request.method == "GET":
        serializer = ProjectSerializer(project)
        return Response(serializer.data)
    
    elif request.method in ["PATCH", "PUT"]:
        serializer = ProjectUpdateSerializer(instance=project, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        
        # Update project fields
        update_fields = []
        for field, value in serializer.validated_data.items():
            setattr(project, field, value)
            update_fields.append(field)
        
        # Save project (this will trigger auto-disable logic if end_date passed)
        project.save(update_fields=update_fields + ["updated_at"])
        
        response_serializer = ProjectSerializer(project)
        return Response(response_serializer.data, status=status.HTTP_200_OK)


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def project_delete(request, project_id):
    """Delete a project.
    
    Args:
        project_id: ID of the project to delete
    
    Returns:
        - Success message
    """
    # Only faculty can delete projects
    if request.user.user_type != UserType.FACULTY:
        return Response(
            {"error": "Only faculty members can delete projects."},
            status=status.HTTP_403_FORBIDDEN,
        )
    
    # Get project and ensure it belongs to the current user
    project = get_object_or_404(Project, id=project_id, faculty=request.user)
    
    project.delete()
    
    return Response(
        {"message": "Project deleted successfully."},
        status=status.HTTP_200_OK,
    )
