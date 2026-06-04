# https://docs.djangoproject.com/en/stable/ref/templates/api/#writing-your-own-context-processors

from django.conf import settings

from app.models import MediaTypes, Sources, Status


def export_vars(request):  # noqa: ARG001
    """Export variables to templates."""
    return {
        "REGISTRATION": getattr(settings, "REGISTRATION", False),
        "REDIRECT_LOGIN_TO_SSO": getattr(settings, "REDIRECT_LOGIN_TO_SSO", False),
        "IMG_NONE": getattr(settings, "IMG_NONE", "/static/img/none.png"),
        "TRACK_TIME": getattr(settings, "TRACK_TIME", 30),
        "BRAND_NAME": getattr(settings, "BRAND_NAME", "Yamtrack"),
    }


def media_enums(request):  # noqa: ARG001
    """Export media enums to templates."""
    return {
        "MediaTypes": MediaTypes,
        "Sources": Sources,
        "Status": Status,
    }
