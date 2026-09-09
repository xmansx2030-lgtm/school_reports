from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("reports", "0132_ai_usage_event")]

    operations = [
        migrations.AddField(
            model_name="erasurerequest",
            name="extended_until",
            field=models.DateTimeField(blank=True, help_text="تمديد واحد لا يتجاوز 30 يوماً إضافياً، مع بيان السبب لصاحب الطلب.", null=True, verbose_name="الموعد بعد التمديد"),
        ),
        migrations.AddField(
            model_name="erasurerequest",
            name="extension_reason",
            field=models.TextField(blank=True, default="", help_text="إلزامي عند تمديد موعد الرد، ويظهر لصاحب الطلب.", verbose_name="سبب التمديد"),
        ),
        migrations.AddField(
            model_name="erasurerequest",
            name="extension_notified_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="تاريخ إشعار صاحب الطلب بالتمديد"),
        ),
    ]
