from django.db import migrations


def register_mizaan(apps, schema_editor):
    ManagedProject = apps.get_model("operations", "ManagedProject")
    ManagedServer = apps.get_model("operations", "ManagedServer")
    server = ManagedServer.objects.filter(slug="school-reports-prod").first()
    if server is None:
        return

    ManagedProject.objects.update_or_create(
        slug="mizaan-beta",
        defaults={
            "server": server,
            "name": "Mizaan Beta",
            "base_url": "https://mizaanlegal.com",
            "health_path": "/",
            "compose_project": "mizaan-beta",
            "repository": "",
            "deploy_branch": "main",
            "ci_workflow": "",
            "deploy_repository": "",
            "deploy_workflow": "",
            "deploy_container": "mizaan-beta-web",
            "deployment_enabled": False,
            "is_active": True,
            "sort_order": 3,
        },
    )
    ManagedProject.objects.filter(slug="school-display").update(sort_order=4)


class Migration(migrations.Migration):
    dependencies = [("operations", "0008_project_runtime_metrics")]

    operations = [migrations.RunPython(register_mizaan, migrations.RunPython.noop)]
