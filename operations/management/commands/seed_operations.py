from django.core.management.base import BaseCommand

from operations.inventory import PROJECTS, SERVER_DEFAULTS, project_defaults
from operations.models import ManagedProject, ManagedServer, ManagedService


class Command(BaseCommand):
    help = "Create the production server/project inventory used by the operations mobile app."

    def handle(self, *args, **options):
        server, _ = ManagedServer.objects.update_or_create(
            slug=SERVER_DEFAULTS["slug"],
            defaults={
                key: value for key, value in SERVER_DEFAULTS.items() if key != "slug"
            } | {
                "is_active": True,
            },
        )
        for order, item in enumerate(PROJECTS, start=1):
            project, _ = ManagedProject.objects.update_or_create(
                slug=item["slug"],
                defaults=project_defaults(item, server=server, sort_order=order),
            )
            for service_key, service_name, kind, restart_allowed in (
                ("web", "تطبيق الويب", ManagedService.Kind.WEB, True),
                ("database", "قاعدة البيانات", ManagedService.Kind.DATABASE, False),
                ("cache", "Redis", ManagedService.Kind.CACHE, True),
            ):
                ManagedService.objects.update_or_create(
                    project=project,
                    service_key=service_key,
                    defaults={"name": service_name, "kind": kind, "restart_allowed": restart_allowed, "is_active": True},
                )
        self.stdout.write(self.style.SUCCESS("Operations inventory is ready."))
