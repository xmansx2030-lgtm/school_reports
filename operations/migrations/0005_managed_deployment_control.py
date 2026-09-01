from django.db import migrations, models


CONTROL_REPOSITORY = "xmansx2030-lgtm/school_reports"


def configure_managed_deployments(apps, schema_editor):
    ManagedProject = apps.get_model("operations", "ManagedProject")
    configurations = {
        "tawtheeq": {
            "ci_workflow": "ci.yml",
            "deploy_repository": CONTROL_REPOSITORY,
            "deploy_workflow": "ci.yml",
            "deployment_enabled": True,
        },
        "xmansx": {
            "ci_workflow": "ci.yml",
            "deploy_repository": CONTROL_REPOSITORY,
            "deploy_workflow": "deploy-tanal.yml",
            "deployment_enabled": True,
        },
        "school-display": {
            "ci_workflow": "ci.yml",
            "deploy_repository": CONTROL_REPOSITORY,
            "deploy_workflow": "deploy-school-display.yml",
            "deployment_enabled": True,
        },
    }
    for slug, values in configurations.items():
        ManagedProject.objects.filter(slug=slug).update(**values)


class Migration(migrations.Migration):
    dependencies = [
        ("operations", "0004_single_user_operations_access"),
    ]

    operations = [
        migrations.AddField(
            model_name="managedproject",
            name="ci_workflow",
            field=models.CharField(blank=True, default="", max_length=120),
        ),
        migrations.AddField(
            model_name="managedproject",
            name="deploy_repository",
            field=models.CharField(blank=True, default="", max_length=160),
        ),
        migrations.RunPython(configure_managed_deployments, migrations.RunPython.noop),
    ]
