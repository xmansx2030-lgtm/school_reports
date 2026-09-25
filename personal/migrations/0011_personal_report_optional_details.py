from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("personal", "0010_portfolio_annual_profile"),
    ]

    operations = [
        migrations.AddField(
            model_name="personalreport",
            name="client_submission_id",
            field=models.UUIDField(null=True, blank=True, unique=True, editable=False),
        ),
        migrations.AlterField(
            model_name="personalreport",
            name="description",
            field=models.TextField("وصف العمل", blank=True),
        ),
        migrations.AddField(
            model_name="personalreport",
            name="show_details",
            field=models.BooleanField("إظهار تفاصيل التقرير", default=True),
        ),
    ]
