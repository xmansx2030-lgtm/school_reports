from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("reports", "0133_erasure_request_sla")]

    operations = [
        migrations.AddField(
            model_name="erasurerequest",
            name="execution_evidence",
            field=models.TextField(
                blank=True,
                default="",
                help_text="إلزامي عند اعتبار الطلب منفذاً: دوّن ما أُتلف وما استُبقي ومرجع الإجراء.",
                verbose_name="محضر التنفيذ الداخلي",
            ),
        ),
    ]
