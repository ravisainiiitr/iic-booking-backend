"""User type constants and helpers.

This module provides constants and helper methods for working with user types.
"""

from django.utils.translation import gettext_lazy as _


class UserType:
    """User type constants and helpers."""

    # User type code constants (lowercase codes for database storage)
    ADMIN = "admin"
    MANAGER = "manager"
    OPERATOR = "operator"
    FINANCE = "finance"

    STUDENT = "student"
    INDIVIDUAL_STUDENT = "individual_student"
    FACULTY = "faculty"
    EXTERNAL = "external"  # Educational Institute
    RND = "RND"  # Govt R&D Organizations
    INSTITUTE = "Industry"  # Institutes
    STARTUP_INCUBATED_IITR = "startup_incubated_iitr"  # Startup incubated at IIT Roorkee
    EXTERNAL_STARTUP_MSME = "external_startup_msme"  # External startup / MSME
    OTHER = "other"  # Other external users

    @classmethod
    def get_choices(cls) -> list[tuple[str, str]]:
        """Get user type choices.
        
        Returns:
            List of (code, name) tuples
        """
        return [
            (cls.ADMIN, _("Admin")),
            (cls.MANAGER, _("Officer In Charge")),
            (cls.OPERATOR, _("Lab Incharge")),
            (cls.FINANCE, _("Accounts In Charge")),
            (cls.STUDENT, _("IITR Student")),
            (cls.INDIVIDUAL_STUDENT, _("Individual Student")),
            (cls.FACULTY, _("IITR Faculty")),
            (cls.EXTERNAL, _("Educational Institute")),
            (cls.RND, _("Govt R&D Organizations")),
            (cls.INSTITUTE, _("Industry")),
            (cls.STARTUP_INCUBATED_IITR, _("Startup Incubated at IIT Roorkee")),
            (cls.EXTERNAL_STARTUP_MSME, _("External Startup/MSME")),
            (cls.OTHER, _("Other")),
        ]

    @classmethod
    def get_admin_panel_codes(cls) -> set[str]:
        """Get user type codes that access the system via admin panel.
        
        Returns:
            Set of user type codes
        """
        return {cls.ADMIN, cls.MANAGER, cls.OPERATOR, cls.FINANCE}

    @classmethod
    def is_end_user_booking_type(cls, user_type: str | None) -> bool:
        """True for student/faculty/external categories that pay via department wallets.

        Staff types (admin, OIC, lab/accounts incharge) are excluded.
        """
        if not user_type:
            return False
        code = str(user_type).strip().lower()
        return code in {
            cls.STUDENT,
            cls.INDIVIDUAL_STUDENT,
            cls.FACULTY,
            cls.EXTERNAL,
            cls.RND.lower(),
            cls.INSTITUTE.lower(),
            cls.STARTUP_INCUBATED_IITR,
            cls.EXTERNAL_STARTUP_MSME,
        }

    @classmethod
    def get_wallet_eligible_codes(cls) -> set[str]:
        """Get user type codes that can have their own individual wallet.
        
        Note: Regular STUDENT uses faculty wallet (not included here).
        All other eligible users get their own individual wallet.
        
        Returns:
            Set of user type codes that can have individual wallets
        """
        return {
            cls.INDIVIDUAL_STUDENT,  # Individual students get their own wallet
            cls.FACULTY,  # Faculty get their own wallet
            cls.EXTERNAL,  # External users get their own wallet
            cls.RND,  # R&D Center users get their own wallet
            cls.INSTITUTE,  # Institute users get their own wallet
            cls.STARTUP_INCUBATED_IITR,
            cls.EXTERNAL_STARTUP_MSME,
            cls.OTHER,  # Other external users get their own wallet
        }
    
    @classmethod
    def get_internal_user_codes(cls) -> set[str]:
        """Get user type codes for internal users.
        
        Returns:
            Set of internal user type codes
        """
        return {cls.STUDENT, cls.INDIVIDUAL_STUDENT, cls.FACULTY, cls.STARTUP_INCUBATED_IITR}
    
    @classmethod
    def get_external_user_codes(cls) -> set[str]:
        """Get user type codes for external users.
        
        Returns:
            Set of external user type codes
        """
        return {cls.EXTERNAL, cls.RND, cls.INSTITUTE, cls.EXTERNAL_STARTUP_MSME, cls.OTHER}
    
    @classmethod
    def get_management_user_codes(cls) -> set[str]:
        """Get user type codes for management users.
        
        Returns:
            Set of management user type codes
        """
        return {cls.ADMIN, cls.MANAGER, cls.OPERATOR, cls.FINANCE}
    
    @classmethod
    def is_internal_user(cls, user_type: str) -> bool:
        """Check if a user type is an internal user.
        
        Args:
            user_type: User type code
            
        Returns:
            bool: True if internal user, False otherwise
        """
        return user_type in cls.get_internal_user_codes()
    
    @classmethod
    def is_external_user(cls, user_type: str) -> bool:
        """Check if a user type is an external user.
        
        Args:
            user_type: User type code
            
        Returns:
            bool: True if external user, False otherwise
        """
        return user_type in cls.get_external_user_codes()
    
    @classmethod
    def is_management_user(cls, user_type: str) -> bool:
        """Check if a user type is a management user.
        
        Args:
            user_type: User type code
            
        Returns:
            bool: True if management user, False otherwise
        """
        return user_type in cls.get_management_user_codes()

    @classmethod
    def get_omniport_codes(cls) -> set[str]:
        """Get user type codes that authenticate via Omniport.
        
        Returns:
            Set of user type codes for Omniport authentication
        """
        return {cls.STUDENT, cls.INDIVIDUAL_STUDENT, cls.FACULTY, cls.MANAGER, cls.OPERATOR, cls.FINANCE}

    @classmethod
    def get_email_auth_codes(cls) -> set[str]:
        """Get user type codes that authenticate via email/password.
        
        Returns:
            Set of user type codes for email authentication
        """
        return {
            cls.EXTERNAL,
            cls.RND,
            cls.INSTITUTE,
            cls.STARTUP_INCUBATED_IITR,
            cls.EXTERNAL_STARTUP_MSME,
            cls.OTHER,
        }