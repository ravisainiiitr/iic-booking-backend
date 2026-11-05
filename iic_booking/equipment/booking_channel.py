"""Where a booking was made: Android app, iOS app, desktop / phone / tablet browser, or the system (scheduled and
background jobs, scripts). Stamped once on ``Booking.created_via`` when the booking is created, for reporting how
the mobile apps change portal use.

* The apps sign in with device-bound mobile sessions (``iicm_`` access keys): the session says Android or iOS.
* The apps also add ``IICBookingApp/`` to the browser identification (user agent), used as a fallback.
* Everything else is classified from the browser identification.
"""

from __future__ import annotations

import re

from django.db import models
from django.utils.translation import gettext_lazy as _


class BookingChannel(models.TextChoices):
    ANDROID_APP = "ANDROID_APP", _("Android app")
    IOS_APP = "IOS_APP", _("iOS app")
    DESKTOP_WEB = "DESKTOP_WEB", _("Desktop browser")
    MOBILE_WEB = "MOBILE_WEB", _("Phone browser")
    TABLET_WEB = "TABLET_WEB", _("Tablet browser")
    API_CLIENT = "API_CLIENT", _("Other client")
    SYSTEM = "SYSTEM", _("System (job or script)")


APP_MARKER = "IICBookingApp/"
_IOS = re.compile(r"iPhone|iPad|iPod|Macintosh|Mac OS X")
_TABLET = re.compile(r"iPad|Tablet")
_MOBILE = re.compile(r"Mobi|iPhone|iPod|Android")
_BROWSER = re.compile(r"Mozilla/|Opera/")


def channel_from_user_agent(ua: str) -> str:
    ua = ua or ""
    if APP_MARKER in ua:
        if "Android" in ua:
            return BookingChannel.ANDROID_APP
        if _IOS.search(ua):
            return BookingChannel.IOS_APP
    if not _BROWSER.search(ua):
        return BookingChannel.API_CLIENT
    if _TABLET.search(ua) or ("Android" in ua and "Mobile" not in ua):
        return BookingChannel.TABLET_WEB
    if _MOBILE.search(ua):
        return BookingChannel.MOBILE_WEB
    return BookingChannel.DESKTOP_WEB


def channel_for_request(request) -> str:
    from iic_booking.users.models.mobile_device_session import MobileDeviceSession

    auth = getattr(request, "auth", None)
    if isinstance(auth, MobileDeviceSession):
        return BookingChannel.IOS_APP if auth.platform == MobileDeviceSession.PLATFORM_IOS else BookingChannel.ANDROID_APP
    return channel_from_user_agent(request.META.get("HTTP_USER_AGENT", "") if hasattr(request, "META") else "")


def current_channel() -> str:
    """The channel of the web request being served, or SYSTEM inside jobs and scripts."""
    from .booking_audit import current_request

    request = current_request()
    return channel_for_request(request) if request is not None else BookingChannel.SYSTEM


def stamp_new_booking(sender, instance, **kwargs) -> None:
    """pre_save: record the channel once, when the booking is created (an explicit value is kept)."""
    if instance._state.adding and not instance.created_via:
        try:
            instance.created_via = current_channel()
        except Exception:  # reporting only: never block a booking
            instance.created_via = ""


def connect_signals() -> None:
    from django.db.models.signals import pre_save

    from .models import Booking

    pre_save.connect(stamp_new_booking, sender=Booking, dispatch_uid="booking_channel_stamp")
