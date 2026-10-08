from allauth.account.forms import SignupForm
from allauth.socialaccount.forms import SignupForm as SocialSignupForm
from django.contrib.auth import forms as admin_forms
from django.forms import BooleanField
from django.forms import EmailField
from django.forms import ModelForm
from django.utils.translation import gettext_lazy as _

from .models import User


class UserAdminChangeForm(admin_forms.UserChangeForm):
    sign_in_with_email = BooleanField(
        label=_("Sign in with email"),
        required=False,
        help_text=_(
            "Allow this user to sign in with email (password or email OTP) in addition to Channel i. "
            "Off by default for IITR students and faculty; on by default for OIC and Lab Operator."
        ),
    )

    class Meta(admin_forms.UserChangeForm.Meta):  # type: ignore[name-defined]
        model = User
        field_classes = {"email": EmailField}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        field = self.fields["sign_in_with_email"]
        user = self.instance
        if user and user.pk and user.has_email_login_toggle():
            field.initial = user.is_email_login_allowed()
        else:
            field.initial = True
            field.disabled = True
            field.help_text = _(
                "Always on: email and password is the only sign-in method for this account type."
            )

    def save(self, commit=True):
        # Only store an explicit choice when the admin changed it, so untouched users keep the user-type default.
        if "sign_in_with_email" in self.changed_data and not self.fields["sign_in_with_email"].disabled:
            self.instance.email_login_enabled = bool(self.cleaned_data.get("sign_in_with_email"))
        return super().save(commit=commit)


class UserAdminCreationForm(admin_forms.AdminUserCreationForm):
    """
    Form for User Creation in the Admin Area.
    To change user signup, see UserSignupForm and UserSocialSignupForm.
    """

    class Meta(admin_forms.UserCreationForm.Meta):  # type: ignore[name-defined]
        model = User
        fields = ("email", "name")
        field_classes = {"email": EmailField}
        error_messages = {
            "email": {"unique": _("This email has already been taken.")},
        }


class UserSignupForm(SignupForm):
    """
    Form that will be rendered on a user sign up section/screen.
    Default fields will be added automatically.
    Check UserSocialSignupForm for accounts created from social.
    """


class UserSocialSignupForm(SocialSignupForm):
    """
    Renders the form when user has signed up using social accounts.
    Default fields will be added automatically.
    See UserSignupForm otherwise.
    """



