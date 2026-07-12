from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.staticfiles.urls import staticfiles_urlpatterns
from django.http import JsonResponse
from django.urls import include
from django.urls import path
from django.views import defaults as default_views
from iic_booking.equipment.views import serve_equipment_image
from iic_booking.users.api.auth_views import obtain_auth_token_single_session


def root_view(request):
    """Root URL handler - returns available endpoints"""
    return JsonResponse({
        "message": "IIC Booking API",
        "admin": f"/{settings.ADMIN_URL}",
        "api": "/api/",
    })


# Only Admin and API endpoints
urlpatterns = [
    # Root URL - simple JSON response
    path("", root_view, name="root"),
    # Django Admin, use {% url 'admin:index' %}
    path(settings.ADMIN_URL, admin.site.urls),
    # Staff-only equipment image (for admin change form preview; same session as admin)
    path("serve-equipment-image/<int:pk>/", serve_equipment_image, name="serve_equipment_image"),
    # Media files (needed for API to serve media)
    *static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT),
]

# Static files (needed for admin interface)
if settings.DEBUG:
    # Static file serving when using Gunicorn + Uvicorn for local web socket development
    urlpatterns += staticfiles_urlpatterns()

# API URLS
urlpatterns += [
    # API base url
    path("api/", include("config.api_router")),
    # DRF auth token (single-session: invalidates previous logins)
    path("api/auth-token/", obtain_auth_token_single_session, name="obtain_auth_token"),
    # User views (including wallet recharge approve/reject web forms)
    path("", include("iic_booking.users.urls")),
]

if settings.DEBUG:
    # This allows the error pages to be debugged during development, just visit
    # these url in browser to see how these error pages look like.
    urlpatterns += [
        path(
            "400/",
            default_views.bad_request,
            kwargs={"exception": Exception("Bad Request!")},
        ),
        path(
            "403/",
            default_views.permission_denied,
            kwargs={"exception": Exception("Permission Denied")},
        ),
        path(
            "404/",
            default_views.page_not_found,
            kwargs={"exception": Exception("Page not Found")},
        ),
        path("500/", default_views.server_error),
    ]
    if "debug_toolbar" in settings.INSTALLED_APPS:
        import debug_toolbar

        urlpatterns = [
            path("__debug__/", include(debug_toolbar.urls)),
            *urlpatterns,
        ]
