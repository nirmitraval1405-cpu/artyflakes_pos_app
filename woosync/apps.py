from django.apps import AppConfig


class WoosyncConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'woosync'
    verbose_name = "Website Sync"

    def ready(self):
        from . import signals
