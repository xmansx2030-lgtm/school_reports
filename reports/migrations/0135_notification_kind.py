from django.db import migrations, models


def classify_existing_notifications(apps, schema_editor):
    Notification = apps.get_model("reports", "Notification")
    Notification.objects.filter(requires_signature=True).update(kind="circular")
    Notification.objects.filter(requires_signature=False).update(kind="notification")


class Migration(migrations.Migration):
    dependencies = [
        ("reports", "0134_erasure_request_execution_evidence"),
    ]

    operations = [
        migrations.AddField(
            model_name="notification",
            name="kind",
            field=models.CharField(
                choices=[
                    ("notification", "إشعار"),
                    ("newsletter", "نشرة"),
                    ("circular", "تعميم"),
                ],
                db_index=True,
                default="notification",
                help_text="يفصل نوع الوثيقة عن طلب التوقيع؛ فقد تتطلب النشرة توقيعًا أو لا تتطلبه.",
                max_length=16,
                verbose_name="نوع التواصل",
            ),
        ),
        migrations.RunPython(classify_existing_notifications, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="notification",
            name="is_broadcast",
            field=models.BooleanField(
                default=False,
                help_text="عند التفعيل يُعرض التواصل لجميع مستلميه دون تخصيص.",
                verbose_name="للجميع (بث عام)؟",
            ),
        ),
        migrations.AlterField(
            model_name="notification",
            name="requires_signature",
            field=models.BooleanField(
                default=False,
                help_text="عند التفعيل تتطلب الوثيقة إقرارًا وإدخال رقم الجوال لاعتماد التوقيع.",
                verbose_name="يتطلب توقيع؟",
            ),
        ),
    ]
