from django.db import migrations


def seed_personal_free_plan(apps, schema_editor):
    plan_model = apps.get_model("personal", "PersonalPlan")
    plan_model.objects.using(schema_editor.connection.alias).get_or_create(
        code="personal_free",
        defaults={
            "name": "المساحة الشخصية الأساسية",
            "description": "ابدأ بتوثيق أعمالك وشواهدك وملف إنجازك السنوي.",
            "price": 0,
            "duration_days": 0,
            "max_reports": 500,
            "max_evidence": 250,
            "storage_limit_mb": 500,
            "is_active": True,
            "is_published": True,
        },
    )


class Migration(migrations.Migration):
    dependencies = [("personal", "0003_personalplan_description_personalplan_display_order_and_more")]

    operations = [migrations.RunPython(seed_personal_free_plan, migrations.RunPython.noop)]
