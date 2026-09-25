from django.db import migrations


def backfill_years(apps, schema_editor):
    Workspace = apps.get_model("personal", "PersonalWorkspace")
    Year = apps.get_model("personal", "PersonalAcademicYear")
    Report = apps.get_model("personal", "PersonalReport")
    Evidence = apps.get_model("personal", "PersonalEvidence")
    db = schema_editor.connection.alias
    for field, flag in (
        ("goals", "show_goals"), ("implementation", "show_implementation"),
        ("results", "show_results"), ("recommendations", "show_recommendations"),
    ):
        Report.objects.using(db).filter(**{field: ""}).update(**{flag: False})
    for workspace in Workspace.objects.using(db).all().iterator(chunk_size=500):
        values = set(Report.objects.using(db).filter(workspace_id=workspace.pk).exclude(
            academic_year=""
        ).values_list("academic_year", flat=True))
        values.update(Evidence.objects.using(db).filter(workspace_id=workspace.pk).exclude(
            academic_year=""
        ).values_list("academic_year", flat=True))
        Year.objects.using(db).bulk_create([
            Year(workspace_id=workspace.pk, value=value) for value in values
        ], ignore_conflicts=True)
        if values and not workspace.current_academic_year:
            Workspace.objects.using(db).filter(pk=workspace.pk).update(current_academic_year=max(values))


class Migration(migrations.Migration):
    dependencies = [("personal", "0007_personalreport_beneficiaries_count_and_more")]
    operations = [migrations.RunPython(backfill_years, migrations.RunPython.noop)]
