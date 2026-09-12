# سجل الدين التقني

آخر مراجعة: 2026-09-12. لا يحتوي هذا السجل على أسرار أو بيانات إنتاج. كل بند
لم يُصلح في هذه المرحلة لأن إصلاحه يحتاج اختبارًا/قرارًا/تحققًا خارج المستودع،
لا لأنه مقبول دائمًا.

## P0 — حرج

لا توجد مشكلة P0 مؤكدة في لقطة المستودع الحالية.

## P1 — عالٍ

| المشكلة | الموقع | الأثر والمخاطر | التوصية ومعيار الإغلاق |
|---|---|---|---|
| أسرار قديمة في تاريخ Git | تاريخ `.env` (الملف الحالي غير متتبع) | حذف الملف من الرأس لا يبطل credentials التي ظهرت تاريخيًا | تدوير كل قيمة ظهرت، التحقق من سجلات المزودين، ثم إعادة كتابة التاريخ في نافذة منسقة وإبطال النسخ القديمة |
| import hubs وwildcard imports | `reports/views/_helpers.py`, `reports/model_parts/base.py`, `reports/views/billing_*.py` | تخفي الاعتماديات، توسع أثر التعديل، وتمنع F401 من تحليل هذه الملفات فقط | نقل كل وحدة إلى imports صريحة على دفعات مع اختبارات المجال؛ إزالة استثناء F401 لكل ملف عند اكتماله |
| دورات استيراد متبقية بين منتجي العمل والمهام | `reports/forms.py`, `model_parts/signals.py`, `web_push.py`, `telegram_alerts.py` وبعض `views/` وnotification/realtime paths | صعوبة الاختبار واحتمال فشل import جزئي أو الحاجة إلى imports داخل الدوال | أُغلق مسارا `file_cleanup ↔ tasks` و`generated_exports ↔ tasks` في `74ad0e1b` و`780d89d2` مع full suite وLinux worker smoke؛ أكمل منتجًا واحدًا كل مرة عبر `task_dispatch.py` وخدمة مجال محمية باختبارات |
| استثناءات عامة/ابتلاع متبقٍ | الملفات المسجلة في `pyproject.toml` تحت S110/S112 | قد يخفي تعطل بطاقة أو تكامل ويقلل قابلية الرصد | التقاط نوع محدد أو استخدام `core.observability` بسياق؛ حذف الاستثناء من القائمة عند تنظيف الملف |

## P2 — متوسط

| المشكلة | الموقع | الأثر والمخاطر | التوصية ومعيار الإغلاق |
|---|---|---|---|
| ملفات Python كبيرة ومتعددة المسؤوليات | `reports/forms.py`, `reports/views/reports.py`, `reports/views/schools.py`, `reports/services_export.py`, `reports/tasks.py`, `config/settings.py` | مراجعة بطيئة وتعقيد مرتفع وتعارضات دمج | تقسيم مجال واحد كل مرة بعد characterization tests، مع واجهات توافق مؤقتة ومقياس حجم/تعقيد بعدي |
| قوالب ضخمة تحمل CSS/JS داخليًا | `my_subscription.html`, `admin_dashboard.html` وقوالب الإدارة الكبيرة | صعوبة اختبار الواجهة وتكرار السلوك/التنسيق | نقل السلوك إلى modules والأجزاء المتكررة إلى includes؛ فحص RTL و390x844 وdesktop وconsole بعد كل شريحة |
| دين CSS و`!important` مرتفع | `static/css/app.css`, `static/css/design-system.css` والقوالب | specificity غير متوقعة وتكلفة تعديل التصميم | جرد selectors فعلي عبر coverage بصري، توحيد tokens والمكوّنات، ثم حذف القواعد غير المستخدمة تدريجيًا |
| لا توجد بوابة typecheck للمشروع | Python وJavaScript غير TypeScript | عقود الدوال المركبة لا تُفحص ساكنًا | ابدأ بخدمات جديدة/حرجة عبر type hints وpyright/mypy في نطاق صغير؛ لا تفعل strict على 100k سطر دفعة واحدة |
| مسارات تكامل لا يثبتها SQLite المحلي | PostgreSQL، Redis locks/rate limits، R2، WeasyPrint، مزودو الدفع | نجاح unit tests لا يثبت سلوك البيئة الفعلية | تشغيل integration gates في Linux/Docker بخدمات معزولة، وsandbox للمزود، وعدم استعمال أسرار الإنتاج |
| عدة خيارات stdin سرية في أمر الإعداد | `deploy/hetzner/apply_runtime_config.py` | جمع أكثر من `--*-from-stdin` في استدعاء واحد غير محدد لأن كل قارئ يستهلك stdin | منع الجمع في argparse أو اعتماد envelope JSON واضح؛ أضف اختبار CLI قبل تغيير العقد التشغيلي |

## P3 — منخفض

| المشكلة | الموقع | الأثر والمخاطر | التوصية ومعيار الإغلاق |
|---|---|---|---|
| قوالب بريد بلا مرجع ثابت مؤكد | `emails/message.html`, `password_changed.html`, `subscription_activated.html`, `subscription_expiry.html` | قد تكون legacy أو مختارة باسم ديناميكي؛ حذفها بلا telemetry مخاطرة | سجل أسماء القوالب المستخدمة في staging/production دورة إصدار، ثم احذف المؤكد فقط |
| أصول شعار مكررة بايتياً | web/PWA و`operations_mobile/assets/` | مساحة صغيرة وتحديث يدوي متعدد | أبقها ما دامت build roots مستقلة؛ إن أضيف build pipeline موحد، ولّد النسخ من أصل واحد |
| مفتاح Firebase Android عميل داخل `google-services.json` | `operations_mobile/android/app/google-services.json` | المفتاح عام بطبيعته لكن سوء قيود API/app يزيد إساءة الاستخدام | تحقق خارجيًا من Android package/SHA وAPI restrictions في Google Cloud/Firebase؛ لا تنقل service-account إلى التطبيق |

## قواعد صيانة السجل

1. لا يغلق بندًا اختبار عابر؛ اذكر الدليل والـcommit الذي أزال سببه.
2. أي بند جديد يحدد موقعًا وأثرًا ومخاطرة وتوصية وأولوية.
3. لا تستخدم هذا الملف لتأجيل إصلاح آمن وصغير يقع ضمن التغيير الحالي.
4. راجع P1 قبل كل إصدار، وP2 في تخطيط كل دورة تطوير.
