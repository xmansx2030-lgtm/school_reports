from django.apps import AppConfig


class ReportsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'reports'
    verbose_name = 'إدارة المنصة'

    def ready(self):
        # These receivers enforce authentication, storage accounting and
        # physical-file lifecycle. A broken import must fail application boot;
        # silently starting without them corrupts security or accounting.
        from . import signals  # noqa: F401
        from . import file_cleanup, storage_tracking

        storage_tracking.connect_all()
        file_cleanup.connect_all()
