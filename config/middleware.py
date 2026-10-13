"""Custom middleware for the IIC booking project."""

from django.middleware.gzip import GZipMiddleware as DjangoGZipMiddleware


class GZipMiddleware(DjangoGZipMiddleware):
    """Django's gzip, except responses marked ``skip_gzip`` (large file downloads): compressing those again
    costs CPU per byte and drops Content-Length, so the browser cannot show download progress."""

    def process_response(self, request, response):
        if getattr(response, "skip_gzip", False):
            return response
        return super().process_response(request, response)


class AdminUnloadPermissionsPolicyMiddleware:
    """
    Allow the deprecated 'unload' event for Django admin pages.

    Django admin uses the unload event (e.g. in RelatedObjectLookups.js) for
    related-object popups. Modern browsers (Chrome 117+) enforce Permissions
    Policy and report "unload is not allowed in this document". Setting
    Permissions-Policy: unload=(self) for admin URLs allows the existing
    admin JS to run without violation until Django switches to pagehide.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.path.startswith("/admin/"):
            # Allow unload for the current origin only (admin pages)
            existing = response.get("Permissions-Policy", "").strip()
            if existing:
                response["Permissions-Policy"] = existing + ", unload=(self)"
            else:
                response["Permissions-Policy"] = "unload=(self)"
        return response
