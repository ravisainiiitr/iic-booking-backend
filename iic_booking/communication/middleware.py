"""Middleware for automatic user tracking in communication models."""

from .utils import set_current_user, clear_current_user


class CommunicationUserMiddleware:
    """
    Middleware to automatically set the current user for communication models.
    
    This allows created_by and updated_by fields to be automatically set
    when creating or updating CommunicationTemplate instances.
    """
    
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Set current user if authenticated
        if hasattr(request, 'user') and request.user.is_authenticated:
            set_current_user(request.user)
        
        try:
            response = self.get_response(request)
        finally:
            # Always clear the current user after request
            clear_current_user()
        
        return response

