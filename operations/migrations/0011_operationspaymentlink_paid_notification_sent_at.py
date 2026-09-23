from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("operations", "0010_operationspaymentlink"),
    ]

    operations = [
        migrations.AddField(
            model_name="operationspaymentlink",
            name="paid_notification_sent_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
