from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("personal", "0013_personal_evidence_presentation"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="personalreport",
            name="trashed_at",
            field=models.DateTimeField("نُقل إلى سلة المحذوفات في", null=True, blank=True, db_index=True),
        ),
        migrations.AddField(
            model_name="personalreport",
            name="trashed_by",
            field=models.ForeignKey(
                to=settings.AUTH_USER_MODEL, on_delete=django.db.models.deletion.SET_NULL,
                null=True, blank=True, related_name="trashed_personal_reports",
                verbose_name="نُقل إلى السلة بواسطة",
            ),
        ),
    ]
