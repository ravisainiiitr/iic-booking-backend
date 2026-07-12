"""Views for Department models."""

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from ..models import Department
from ..serializers import (
    DepartmentSerializer,
    DepartmentListSerializer,
    OrganizationRequestCreateSerializer,
)
from ..repositories import DepartmentRepository, UserRepository
from ..models.organization_request import OrganizationRequest
from ..models.department import (
    DepartmentType,
    ExternalDepartmentSubcategory,
)


@api_view(["GET"])
@permission_classes([AllowAny])
def department_list(request):
    """Get list of all departments.

    This endpoint is public and can be accessed without authentication.
    Useful for registration forms and public department listings.
    
    Query Parameters:
        - type: Filter by department type (internal, external)
        - group_by_type: If true, returns departments grouped by type
        - group_by_subcategory: When type=external and state is set, return separate lists per subcategory (Educational Institute, Govt R&D, Industry) for that state
        - external_subcategory: When type=external, filter by subcategory (educational_institute, govt_rnd, industries)
        - state: When type=external, filter by Indian state/UT value (e.g. andhra_pradesh)
        - internal_subcategory: When type=internal, filter by subcategory (iit_roorkee_dept_centres, startups)

    Returns:
        Response: List of departments; or by_subcategory when group_by_subcategory=true (separate list per Educational Institute, Govt R&D, Industry per state).
    """
    # Get filter parameters
    department_type_filter = request.query_params.get('type')
    external_subcategory = request.query_params.get('external_subcategory')  # educational_institute | govt_rnd | industries
    state = request.query_params.get('state')  # Indian state/UT value
    internal_subcategory = request.query_params.get('internal_subcategory')  # iit_roorkee_dept_centres | startups
    group_by_subcategory = request.query_params.get('group_by_subcategory', 'false').lower() == 'true'

    # Separate lists per subcategory for each state: when type=external and state provided
    if (
        group_by_subcategory
        and department_type_filter == DepartmentType.EXTERNAL
        and state
    ):
        by_subcategory = {}
        for subcat_key, subcat_label in [
            (ExternalDepartmentSubcategory.EDUCATIONAL_INSTITUTE, "educational_institute"),
            (ExternalDepartmentSubcategory.GOVT_RND, "govt_rnd"),
            (ExternalDepartmentSubcategory.INDUSTRIES, "industries"),
            (ExternalDepartmentSubcategory.EXTERNAL_STARTUP_MSME, "external_startup_msme"),
        ]:
            depts = DepartmentRepository.get_all().filter(
                department_type=DepartmentType.EXTERNAL,
                external_subcategory=subcat_key,
                state=state,
            ).order_by("name")
            dept_data = DepartmentListSerializer(depts, many=True).data
            for d in dept_data:
                d["verified"] = True
            pending = [
                {"id": r.id, "name": r.name, "verified": False}
                for r in OrganizationRequest.objects.filter(
                    state=state,
                    external_subcategory=subcat_key,
                    status=OrganizationRequest.Status.PENDING,
                ).order_by("name")
            ]
            by_subcategory[subcat_label] = {
                "departments": list(dept_data),
                "pending_organization_requests": pending,
                "count": len(dept_data),
            }
        return Response(
            {
                "state": state,
                "by_subcategory": by_subcategory,
            },
            status=status.HTTP_200_OK,
        )

    # Get all departments (single list, optionally filtered)
    if department_type_filter:
        departments = DepartmentRepository.get_all().filter(department_type=department_type_filter)
        # For external type, optionally filter by external_subcategory and state (for signup: Educational Institute, Govt R&D, Industry)
        if department_type_filter == DepartmentType.EXTERNAL:
            if external_subcategory:
                departments = departments.filter(external_subcategory=external_subcategory)
            if state:
                departments = departments.filter(state=state)
        # For internal type, optionally filter by internal_subcategory (for signup: IITR Startups, Post Doc, Research Associates)
        elif department_type_filter == DepartmentType.INTERNAL:
            if internal_subcategory:
                departments = departments.filter(internal_subcategory=internal_subcategory)
    else:
        departments = DepartmentRepository.get_all()
    
    serializer = DepartmentListSerializer(departments, many=True)
    departments_data = list(serializer.data)

    # All departments in the list are approved/verified (they exist in the system). Mark them for signup UI.
    for d in departments_data:
        d["verified"] = True

    # For Govt R&D signup: append pending organization requests (unverified) so user can request new org.
    pending_organization_requests = []
    if (
        department_type_filter == DepartmentType.EXTERNAL
        and external_subcategory == ExternalDepartmentSubcategory.GOVT_RND
        and state
    ):
        pending = OrganizationRequest.objects.filter(
            state=state,
            external_subcategory=ExternalDepartmentSubcategory.GOVT_RND,
            status=OrganizationRequest.Status.PENDING,
        ).order_by("name")
        pending_organization_requests = [
            {"id": r.id, "name": r.name, "verified": False}
            for r in pending
        ]

    # Check if grouping is requested
    group_by_type = request.query_params.get('group_by_type', 'false').lower() == 'true'
    
    if group_by_type:
        # Group departments by type (internal, external only; equipment uses EquipmentCategory)
        grouped = {
            DepartmentType.INTERNAL: [],
            DepartmentType.EXTERNAL: [],
        }
        for dept_data in departments_data:
            dept_type = dept_data.get('department_type', DepartmentType.INTERNAL)
            if dept_type in grouped:
                grouped[dept_type].append(dept_data)
        
        return Response(
            {
                "departments": departments_data,
                "grouped": grouped,
                "count": len(departments_data),
                "pending_organization_requests": pending_organization_requests,
            },
            status=status.HTTP_200_OK,
        )
    
    return Response(
        {
            "departments": departments_data,
            "count": len(departments_data),
            "pending_organization_requests": pending_organization_requests,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def department_detail(request, pk):
    """Get details of a specific department.

    Args:
        pk: Primary key of the department

    Returns:
        Response: Department details
    """
    department = DepartmentRepository.get_by_id(pk)
    if not department:
        return Response(
            {"error": "Department not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    serializer = DepartmentSerializer(department)
    return Response(serializer.data, status=status.HTTP_200_OK)

@api_view(["POST"])
@permission_classes([AllowAny])
def request_organization(request):
    """
    Public endpoint to create an OrganizationRequest when user
    cannot find their external organization in the dropdown.
    """
    serializer = OrganizationRequestCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    obj = serializer.save()
    return Response(
        {
            "id": obj.id,
            "message": "Organization request submitted. Admin will review and add it to the list.",
        },
        status=status.HTTP_201_CREATED,
    )

