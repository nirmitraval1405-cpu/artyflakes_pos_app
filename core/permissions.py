from django.contrib.auth.mixins import UserPassesTestMixin


class AdminRequiredMixin(UserPassesTestMixin):
    """Use on class-based views that only admins may open."""

    def test_func(self):
        return self.request.user.is_authenticated and self.request.user.is_admin_role