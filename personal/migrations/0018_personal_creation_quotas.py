from datetime import datetime, time

from django.db import migrations, models
from django.utils import timezone


def backfill_quota_usage(apps, schema_editor):
    subscription_model = apps.get_model("personal", "PersonalSubscription")
    report_model = apps.get_model("personal", "PersonalReport")
    evidence_model = apps.get_model("personal", "PersonalEvidence")
    alias = schema_editor.connection.alias
    for subscription in subscription_model.objects.using(alias).all().iterator():
        since = timezone.make_aware(datetime.combine(subscription.start_date, time.min))
        reports = report_model.objects.using(alias).filter(workspace_id=subscription.workspace_id)
        evidence = evidence_model.objects.using(alias).filter(workspace_id=subscription.workspace_id)
        subscription_model.objects.using(alias).filter(pk=subscription.pk).update(
            quota_started_at=since,
            quota_report_after_id=reports.filter(created_at__lt=since).order_by("-pk").values_list("pk", flat=True).first() or 0,
            quota_evidence_after_id=evidence.filter(created_at__lt=since).order_by("-pk").values_list("pk", flat=True).first() or 0,
            reports_created=reports.filter(created_at__gte=since).count(),
            evidence_created=evidence.filter(created_at__gte=since).count(),
        )


class Migration(migrations.Migration):
    dependencies = [("personal", "0017_personal_assistant_plan_limits")]

    operations = [
        migrations.AddField(
            model_name="personalsubscription", name="quota_started_at",
            field=models.DateTimeField(default=timezone.now, verbose_name="بداية احتساب حدود الإنشاء"),
        ),
        migrations.AddField(
            model_name="personalsubscription", name="reports_created",
            field=models.PositiveIntegerField(default=0, verbose_name="التقارير المنشأة خلال الاشتراك"),
        ),
        migrations.AddField(
            model_name="personalsubscription", name="evidence_created",
            field=models.PositiveIntegerField(default=0, verbose_name="الشواهد المنشأة خلال الاشتراك"),
        ),
        migrations.AddField(
            model_name="personalsubscription", name="quota_report_after_id",
            field=models.PositiveBigIntegerField(default=0, editable=False),
        ),
        migrations.AddField(
            model_name="personalsubscription", name="quota_evidence_after_id",
            field=models.PositiveBigIntegerField(default=0, editable=False),
        ),
        migrations.RunPython(backfill_quota_usage, migrations.RunPython.noop),
    ]
