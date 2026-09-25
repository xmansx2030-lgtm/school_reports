from django.db import migrations, models


def backfill_report_evidence_order(apps, schema_editor):
    Evidence = apps.get_model("personal", "PersonalEvidence")
    database = schema_editor.connection.alias
    rows = Evidence.objects.using(database).filter(report_id__isnull=False).order_by(
        "report_id", "-created_at", "-id",
    ).only("id", "report_id", "created_at")
    current_report_id = None
    position = 0
    batch = []
    for item in rows.iterator(chunk_size=1000):
        if item.report_id != current_report_id:
            current_report_id = item.report_id
            position = 0
        position += 1
        item.order = position
        batch.append(item)
        if len(batch) == 500:
            Evidence.objects.using(database).bulk_update(batch, ["order"])
            batch.clear()
    if batch:
        Evidence.objects.using(database).bulk_update(batch, ["order"])


class Migration(migrations.Migration):
    dependencies = [
        ("personal", "0012_initiative_best_practice"),
    ]

    operations = [
        migrations.AddField(
            model_name="personalevidence",
            name="order",
            field=models.PositiveSmallIntegerField("الترتيب", default=1, db_index=True),
        ),
        migrations.AddField(
            model_name="personalevidence",
            name="display_size",
            field=models.CharField(
                "حجم العرض", max_length=10,
                choices=[("auto", "تلقائي"), ("large", "كبير"), ("medium", "متوسط"), ("small", "صغير")],
                default="auto",
            ),
        ),
        migrations.AddField(
            model_name="personalevidence",
            name="fit_mode",
            field=models.CharField(
                "طريقة الملاءمة", max_length=10,
                choices=[("contain", "احتواء الصورة كاملة"), ("cover", "ملء الإطار")],
                default="contain",
            ),
        ),
        migrations.AddField(
            model_name="personalevidence",
            name="show_in_print",
            field=models.BooleanField("إظهار في الطباعة", default=True),
        ),
        migrations.RunPython(backfill_report_evidence_order, migrations.RunPython.noop),
    ]
