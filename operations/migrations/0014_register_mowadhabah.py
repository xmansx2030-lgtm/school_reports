from django.db import migrations


def register_mowadhabah(apps, schema_editor):
    server = apps.get_model("operations", "ManagedServer").objects.filter(slug="school-reports-prod").first()
    if server is None:
        return
    apps.get_model("operations", "ManagedProject").objects.update_or_create(
        slug="mowadhabah",
        defaults={
            "server": server,
            "name": "منصة مواظبة",
            "base_url": "https://mowadhabah.com",
            "health_path": "/api/v1/health/",
            "compose_project": "mowadhabah",
            "repository": "xmansx2030-lgtm/xmansx",
            "deploy_repository": "xmansx2030-lgtm/xmansx",
            "deploy_branch": "main",
            "ci_workflow": "ci.yml",
            "deploy_workflow": "",
            "deploy_container": "mowadhabah-backend-1",
            "deployment_enabled": False,
            "is_active": True,
            "sort_order": 5,
        },
    )


class Migration(migrations.Migration):
    dependencies = [("operations", "0013_monitoring_sources")]
    operations = [migrations.RunPython(register_mowadhabah, migrations.RunPython.noop)]
