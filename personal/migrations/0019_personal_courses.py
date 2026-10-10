from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("personal", "0018_personal_creation_quotas")]

    operations = [
        migrations.CreateModel(
            name="PersonalCourse",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("academic_year", models.CharField(max_length=20, verbose_name="السنة الدراسية")),
                ("title", models.CharField(max_length=200, verbose_name="عنوان الدورة")),
                ("provider", models.CharField(max_length=200, verbose_name="الجهة المقدمة")),
                ("start_date", models.DateField(verbose_name="تاريخ البدء")),
                ("completion_date", models.DateField(blank=True, null=True, verbose_name="تاريخ الإكمال")),
                ("hours", models.DecimalField(decimal_places=2, max_digits=6, validators=[MinValueValidator(Decimal("0.01"))], verbose_name="الساعات التدريبية")),
                ("description", models.TextField(blank=True, verbose_name="المحتوى والأثر المهني")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("certificate", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="course_certificates", to="personal.personalevidence", verbose_name="شهادة الدورة")),
                ("workspace", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="courses", to="personal.personalworkspace")),
            ],
            options={
                "verbose_name": "دورة شخصية",
                "verbose_name_plural": "الدورات الشخصية",
                "ordering": ["-start_date", "-id"],
                "indexes": [models.Index(fields=["workspace", "academic_year"], name="personal_course_ws_year")],
                "constraints": [
                    models.CheckConstraint(condition=models.Q(hours__gt=0), name="personal_course_positive_hours"),
                    models.CheckConstraint(condition=models.Q(completion_date__isnull=True) | models.Q(completion_date__gte=models.F("start_date")), name="personal_course_valid_dates"),
                ],
            },
        ),
    ]
