from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("reports", "0136_circular_issue_and_ack_evidence")]

    operations = [
        migrations.AddField(
            model_name="notificationrecipient",
            name="signature_image",
            field=models.FileField(blank=True, editable=False, upload_to="circular_signatures/"),
        ),
        migrations.AddField(
            model_name="notificationrecipient",
            name="signature_image_sha256",
            field=models.CharField(blank=True, default="", editable=False, max_length=64),
        ),
        migrations.AlterField(
            model_name="notification",
            name="requires_signature",
            field=models.BooleanField(default=False, help_text="عند التفعيل تتطلب الوثيقة إقرارًا وتوقيعًا مرسومًا من المستلم.", verbose_name="يتطلب توقيع؟"),
        ),
    ]
