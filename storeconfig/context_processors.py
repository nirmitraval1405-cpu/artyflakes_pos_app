from .models import StoreSettings


def store(request):
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}
    settings = StoreSettings.load()
    return {
        "store_name": settings.name,
        "store_logo_url": settings.logo.url if settings.logo else "",
    }