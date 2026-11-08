from .models import StoreSettings


def store(request):
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}
    return {"store_name": StoreSettings.load().name}