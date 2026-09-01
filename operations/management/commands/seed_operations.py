from django.core.management.base import BaseCommand

from operations.models import ManagedProject, ManagedServer, ManagedService


class Command(BaseCommand):
    help = "Create the production server/project inventory used by the operations mobile app."

    def handle(self, *args, **options):
        server, _ = ManagedServer.objects.update_or_create(
            slug="school-reports-prod",
            defaults={
                "name": "school-reports-prod",
                "provider": "hetzner",
                "provider_server_id": "155662703",
                "public_ip": "178.104.163.3",
                "server_type": "CPX32",
                "is_active": True,
            },
        )
        projects = (
            {
                "slug": "tawtheeq",
                "name": "منصة توثيق",
                "url": "https://tawtheeq-ksa.com",
                "path": "/healthz/",
                "repository": "xmansx2030-lgtm/school_reports",
                "ci_workflow": "ci.yml",
                "deploy_repository": "xmansx2030-lgtm/school_reports",
                "workflow": "ci.yml",
                "container": "school-reports-web-1",
                "deployment_enabled": True,
            },
            {
                "slug": "xmansx",
                "name": "منصة TANAL",
                "url": "https://xmansx.com",
                "path": "/api/health/readiness",
                "repository": "azzam1122112-dot/Tanal-Barbershop-Interface",
                "ci_workflow": "ci.yml",
                "deploy_repository": "azzam1122112-dot/Tanal-Barbershop-Interface",
                "workflow": "deploy.yml",
                "container": "tanal-web-1",
                "deployment_enabled": True,
            },
            {
                "slug": "school-display",
                "name": "لوحة العرض المدرسية",
                "url": "https://school-display.com",
                "path": "/",
                "repository": "azzam1122112-dot/school_display",
                "ci_workflow": "ci.yml",
                "deploy_repository": "azzam1122112-dot/school_display",
                "workflow": "deploy.yml",
                "container": "school-display-web-1",
                "deployment_enabled": True,
            },
        )
        for order, item in enumerate(projects, start=1):
            project, _ = ManagedProject.objects.update_or_create(
                slug=item["slug"],
                defaults={
                    "server": server,
                    "name": item["name"],
                    "base_url": item["url"],
                    "health_path": item["path"],
                    "repository": item["repository"],
                    "deploy_branch": "main",
                    "ci_workflow": item["ci_workflow"],
                    "deploy_repository": item["deploy_repository"],
                    "deploy_workflow": item["workflow"],
                    "deploy_container": item["container"],
                    "deployment_enabled": item["deployment_enabled"],
                    "sort_order": order,
                    "is_active": True,
                },
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
