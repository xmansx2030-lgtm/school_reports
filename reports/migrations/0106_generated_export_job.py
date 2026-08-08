from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import reports.model_parts.billing


class Migration(migrations.Migration):
    dependencies = [
        ("reports", "0105_storage_option_bucket"),
    ]

    operations = [
        migrations.CreateModel(
            name="GeneratedExportJob",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("kind", models.CharField(choices=[("school_zip", "تصدير المدرسة ZIP"), ("year_zip", "تصدير السنة ZIP"), ("archive_snapshot", "نسخة أرشيف محفوظة")], db_index=True, max_length=32)),
                ("status", models.CharField(choices=[("queued", "في قائمة الانتظار"), ("running", "قيد الإنشاء"), ("ready", "جاهز"), ("failed", "تعذر الإنشاء"), ("expired", "انتهت الصلاحية")], db_index=True, default="queued", max_length=16)),
                ("parameters", models.JSONField(blank=True, default=dict)),
                ("artifact_file", models.FileField(blank=True, max_length=500, upload_to=reports.model_parts.billing.generated_export_upload_to)),
                ("filename", models.CharField(blank=True, default="", max_length=255)),
                ("content_type", models.CharField(blank=True, default="application/zip", max_length=100)),
                ("size_bytes", models.PositiveBigIntegerField(default=0)),
                ("error_message", models.CharField(blank=True, default="", max_length=500)),
                ("expires_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("archive", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="generation_jobs", to="reports.schoolyeararchive")),
                ("requested_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="generated_export_jobs", to=settings.AUTH_USER_MODEL)),
                ("school", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="generated_export_jobs", to="reports.school")),
            ],
            options={"ordering": ("-created_at", "-id")},
        ),
        migrations.AddIndex(
            model_name="generatedexportjob",
            index=models.Index(fields=["requested_by", "status", "-created_at"], name="reports_gej_user_status_idx"),
        ),
        migrations.AddIndex(
            model_name="generatedexportjob",
            index=models.Index(fields=["school", "kind", "status"], name="reports_gej_school_kind_idx"),
        ),
    ]
