from urllib.parse import urlencode

from django.contrib.auth import views as auth_views
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.csrf import csrf_failure as default_csrf_failure


class StaffLoginView(auth_views.LoginView):
    template_name = "accounts/login.html"

    def form_valid(self, form):
        response = super().form_valid(form)
        if self.request.POST.get("remember_me") == "on":
            self.request.session.set_expiry(14 * 24 * 60 * 60)
        else:
            self.request.session.set_expiry(0)
        return response


def csrf_failure(request, reason=""):
    if request.method == "POST" and request.path_info == reverse("accounts:login"):
        query = {"csrf_retry": "1"}
        next_url = request.GET.get("next", "")
        if next_url and url_has_allowed_host_and_scheme(
            next_url,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        ):
            query["next"] = next_url
        return redirect(f"{reverse('accounts:login')}?{urlencode(query)}")

    return default_csrf_failure(request, reason)
