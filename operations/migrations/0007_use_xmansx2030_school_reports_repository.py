from django.db import migrations


NEW_REPOSITORY = "xmansx2030-lgtm/school_reports"


def use_current_school_reports_repository(apps, schema_editor):
    ManagedProject = apps.get_model("operations", "ManagedProject")
    ManagedProject.objects.filter(slug="tawtheeq").update(
        repository=NEW_REPOSITORY,
        deploy_repository=NEW_REPOSITORY,
        ci_workflow="ci.yml",
        deploy_workflow="ci.yml",
        deploy_branch="main",
    )


class Migration(migrations.Migration):
    dependencies = [
        ("operations", "0006_project_owned_deployment_workflows"),
    ]

    operations = [
        migrations.RunPython(
            use_current_school_reports_repository,
            migrations.RunPython.noop,
        ),
    ]
