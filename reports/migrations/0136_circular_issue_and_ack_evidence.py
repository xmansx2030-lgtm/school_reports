from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("reports", "0135_notification_kind"),
    ]

    operations = [
        migrations.AddField(
            model_name="notification",
            name="issued_snapshot",
            field=models.JSONField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="notification",
            name="issued_digest",
            field=models.CharField(blank=True, default="", editable=False, max_length=64),
        ),
        migrations.AddField(
            model_name="notificationrecipient",
            name="signed_document_digest",
            field=models.CharField(blank=True, default="", editable=False, max_length=64),
        ),
        migrations.AddField(
            model_name="notificationrecipient",
            name="signed_ack_text",
            field=models.TextField(blank=True, default="", editable=False),
        ),
        migrations.AddField(
            model_name="notificationrecipient",
            name="signature_method",
            field=models.CharField(blank=True, default="", editable=False, max_length=32),
        ),
        migrations.AddField(
            model_name="notificationrecipient",
            name="signature_evidence_digest",
            field=models.CharField(blank=True, default="", editable=False, max_length=64),
        ),
    ]
