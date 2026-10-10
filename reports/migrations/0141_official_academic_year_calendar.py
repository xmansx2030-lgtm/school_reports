"""Preserve every historical label; publish only verified Ministry calendars.

Verified 2026-10-10 from the datasource used by the Ministry calendar page:
https://www.moe.gov.sa/ar/education/generaleducation/Pages/AcademicCalendar.aspx
DataSources/AcademicCalendar.aspx?Year=1447: start 24/08/2025.
DataSources/AcademicCalendar.aspx?Year=1448: end 25/06/2026; start 23/08/2026.
DataSources/AcademicCalendar.aspx?Year=1449: end 24/06/2027.
The end event explicitly means the end of Thursday's work, so it is inclusive.
"""
import datetime

from django.db import migrations, models


def publish_verified_calendars(apps, schema_editor):
    year_model = apps.get_model("reports", "AcademicYear")
    # The old seed generated estimated future labels. Keep them as drafts.
    year_model.objects.using(schema_editor.connection.alias).update(is_active=False)
    source = "https://www.moe.gov.sa/ar/education/generaleducation/Pages/AcademicCalendar.aspx"
    for value, starts, ends in (
        ("1447-1448", datetime.date(2025, 8, 24), datetime.date(2026, 6, 25)),
        ("1448-1449", datetime.date(2026, 8, 23), datetime.date(2027, 6, 24)),
    ):
        year_model.objects.using(schema_editor.connection.alias).update_or_create(
            value=value,
            defaults={"start_date": starts, "end_date": ends, "source_url": source, "is_active": True},
        )


class Migration(migrations.Migration):
    dependencies = [("reports", "0140_teacher_gender_for_personal_accounts")]

    operations = [
        migrations.AddField(model_name="academicyear", name="start_date", field=models.DateField(blank=True, null=True, verbose_name="بداية الدراسة المعتمدة")),
        migrations.AddField(model_name="academicyear", name="end_date", field=models.DateField(blank=True, null=True, verbose_name="نهاية العام الدراسي المعتمدة")),
        migrations.AddField(model_name="academicyear", name="source_url", field=models.URLField(blank=True, default="", max_length=500, verbose_name="مصدر تقويم وزارة التعليم")),
        migrations.AlterField(model_name="academicyear", name="is_active", field=models.BooleanField(default=False, verbose_name="منشورة للمدارس والمعلمين")),
        migrations.RunPython(publish_verified_calendars, migrations.RunPython.noop),
    ]
