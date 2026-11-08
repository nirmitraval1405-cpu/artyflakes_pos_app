from django.contrib import messages
from django.urls import reverse_lazy
from django.views.generic import UpdateView

from core.permissions import AdminRequiredMixin

from .forms import StoreSettingsForm
from .models import StoreSettings


class StoreSettingsView(AdminRequiredMixin, UpdateView):
    form_class = StoreSettingsForm
    template_name = "storeconfig/settings.html"
    success_url = reverse_lazy("storeconfig:store")

    def get_object(self, queryset=None):
        return StoreSettings.load()

    def form_valid(self, form):
        form.instance.updated_by = self.request.user
        messages.success(self.request, "Store settings saved.")
        return super().form_valid(form)