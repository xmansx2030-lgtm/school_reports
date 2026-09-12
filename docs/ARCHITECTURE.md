# معمارية منصة توثيق

## تدفق الطلب

1. يصل HTTP إلى تطبيق ASGI في `config/asgi.py`.
2. يمر الطلب عبر middleware الحماية والتتبع والجلسة والمدرسة النشطة.
3. تعرض `reports/views/` واجهة المجال المطلوبة.
4. تستعمل العروض خدمات المجال في `reports/services_*.py`.
5. تمر كل استعلامات البيانات المدرسية ضمن نطاق `School`.
6. تنقل العمليات الثقيلة إلى Celery، وتصل تحديثات العدادات عبر Channels.

## حدود المجالات

| المجال | النماذج | العروض والخدمات |
|---|---|---|
| الهوية والمدارس | `model_parts/schools.py` | `views/auth.py`, `views/schools.py` |
| التقارير | `model_parts/reports.py` | `views/reports.py`, `services_reports.py` |
| ملفات الإنجاز | `model_parts/achievements.py` | `views/achievements.py`, `services_achievement.py` |
| التذاكر | `model_parts/tickets.py` | `views/tickets.py` |
| الإشعارات | `model_parts/notifications.py` | `views/notifications.py`, `tasks.py` |
| الاشتراكات والدفع | `model_parts/billing.py` | `views/billing_*.py`, `views/subscriptions.py` |
| التكليفات والخطط | `model_parts/assignments.py`, `model_parts/plans.py` | `views/assignments.py`, `services_assignments.py`, `services_plans.py` |
| الاجتماعات والوثائق | `model_parts/meetings.py`, `model_parts/documents.py` | `views/meetings.py`, `views/documents.py` |
| عمليات المنصة | `operations/models.py` | `operations/views.py`, تطبيق `operations_mobile/` |
| الصيانة | `maintenance/models.py` | `maintenance/services.py` |

يظل `reports/models.py` واجهة التوافق الوحيدة التي تستورد منها بقية المنصة.

## نقاط الدخول والعقود العامة

- `manage.py` و`config/wsgi.py` و`config/asgi.py`: تشغيل Django؛ ASGI هو مدخل
  HTTP وWebSocket في الإنتاج.
- `config/urls.py` ثم `reports/urls.py`: عقد المسارات للويب وPWA والعملاء.
- `reports/api_urls.py` و`operations/urls.py`: واجهات JSON المستخدمة خارجيًا؛
  لا تغيّر الحقول أو حالات HTTP دون اختبار توافق العميل.
- `config/celery.py` و`reports/tasks.py`: أغلفة تنفيذ المهام، بينما تحفظ
  `reports/task_names.py` الأسماء العامة الثابتة، ويرسل المنتجون المستقلون عبر
  `reports/task_dispatch.py`. منطق إنشاء التصدير في
  `reports/services_generated_exports.py`، وتحدد الإعدادات الجدولة في
  `CELERY_BEAT_SCHEDULE`.
- `reports/consumers.py` و`reports/routing.py`: تحديثات WebSocket.
- `reports/static/manifest.json` وservice worker: عقد تثبيت PWA والتخزين المؤقت.

## اتجاه الاعتماديات

الاتجاه المستهدف هو:

```text
HTTP/API views -> permissions/forms/serializers -> domain services -> models
background tasks -------------------------------> domain services -> models
work producers -> task dispatch -> background task -> domain service
templates/static <- context/view models (لا منطق أعمال أو ORM داخل القالب)
```

يجوز للخدمة استدعاء client خارجي محدد (`*_gateway.py`, `email_backends.py`,
`web_push.py`) بمهلة ورسالة خطأ سياقية. لا ينبغي للخدمة أن تستورد view، ولا
للنموذج أن يستورد task. واجهتا التوافق `reports/models.py` و
`reports/views/_helpers.py` قديمتان؛ لا توسّع wildcard imports فيهما.

## رحلات الأعمال الحرجة

- الهوية: login/passkey/TOTP ثم اختيار المدرسة النشطة والتحقق منها في
  middleware. أي إعادة هيكلة تحتاج اختبارات auth، rate limits، وعزل الجلسة.
- المدفوعات: إنشاء طلب محلي، إنشاء فاتورة المزود، تحقق callback من المزود، ثم
  تطبيق الآثار مرة واحدة داخل transaction. لا تعتمد على redirect نجاح العميل.
- التقارير/الإنجاز: تحقق الدور والمدرسة، حفظ الأصل والشواهد، ثم توليد PDF/export
  محليًا أو عبر job. عقود أسماء الملفات وJSON مستخدمة من الواجهة.
- الإشعارات: إنشاء السجل والمستلمين أولًا، ثم dispatch عبر Celery/Web Push/FCM؛
  retry لا يجوز أن يكرر المستلمين أو يسرب بيانات مدرسة أخرى.
- الأرشفة والصيانة: preview أولًا، ثم تنفيذ صريح مسجل audit؛ النسخ السنوية لها
  سعة مستقلة عن مساحة العمل.

## المعاملات وإعادة التنفيذ

- ضع تحديثات الاشتراك والدفع متعددة الجداول داخل `transaction.atomic` مع قفل
  السجل المناسب.
- callbacks والمهام تستخدم مفاتيح provider/dedup وحالة `effects_applied_at` أو
  ما يعادلها؛ أعد الاختبار بطلب مكرر قبل تعديلها.
- شغّل المهام بعد commit عند اعتمادها على صف جديد (`transaction.on_commit`).
- عند عدم حاجة المنتج إلى كائن المهمة نفسه، استخدم الاسم الثابت و
  `task_dispatch.enqueue_named_task` بدل استيراد `reports.tasks`؛ أبقِ غلاف
  Celery رقيقًا وانقل منطق العمل إلى service قابلة للاختبار المباشر.
- لا تجعل Redis مصدر صحة وحيدًا؛ الأقفال تقلل العمل المكرر، والـDB يحفظ العقد.

## ثوابت العزل والصلاحيات

- `SchoolMembership` هو مصدر الدور داخل المدرسة.
- `request.active_school` هو نطاق الطلب المدرسي الحالي.
- لا يجوز قبول `active_school_id` من الجلسة دون تحقق
  `ActiveSchoolGuardMiddleware`.
- لوحة إدارة المنصة مقصورة على مالك النظام (`is_superuser`).
- كل QuerySet جديد يجب أن يضيف مرشح المدرسة قبل الفلاتر والبحث والترقيم.
- روابط المشاركة العامة تمر عبر `ShareLink` منتهي الصلاحية، وليس رابط الملف
  الدائم.

## الملفات والخصوصية

- R2 خاص افتراضيًا.
- `MEDIA_PUBLIC_ACCESS_ENABLED=False` يفرض روابط موقعة حتى لو بقي إعداد قديم
  `AWS_QUERYSTRING_AUTH=0`.
- لا يستخدم النطاق العام إلا عند تفعيل الوصول العام صراحة.
- أسماء التخزين التاريخية مثل `PublicRawMediaStorage` باقية لتوافق الهجرات ولا
  تعني أن الملفات عامة.

## الكاش والمهام

- Redis DB 0: broker وChannels.
- Redis DB 1: كاش Django والجلسات والأقفال.
- الطوابير: `default`, `notifications`, `images`, `periodic`.
- يجب حماية المهام الدورية بقفل قصير، وجعل المهام قابلة لإعادة التنفيذ بأمان.

## استراتيجية الاختبار

- اختبارات characterization تحمي السلوك القديم قبل النقل أو التقسيم.
- اختبارات الصلاحيات يجب أن تغطي السماح والمنع والعزل بين مدرستين.
- اختبارات الدفع تغطي callback مكررًا، mismatch، وحالة فشل المزود.
- اختبارات القوالب/الرحلات تتحقق من endpoints والنصوص الأساسية، لكن لا تستبدل
  فحص DOM والـviewport والـconsole للواجهة.
- `config/test_settings.py` يعزل الخدمات الخارجية ويستخدم SQLite؛ لذلك تبقى
  خصائص PostgreSQL/Redis وPDF الأصلية ضمن CI/Docker والاختبارات التكاملية.

## أين يضاف الكود الجديد

- نموذج مجال: ملفه داخل `reports/model_parts/` مع تصدير توافق إن لزم.
- قراءة مركبة/استعلام reusable: selector أو service المجال، لا نسخة داخل views.
- تغيير حالة/سير عمل: service مع transaction واختبارات مباشرة.
- حارس وصول متكرر: `reports/permissions.py` أو `reports/view_access.py`.
- تكامل HTTP خارجي: client/gateway واحد يملك timeout والتحقق والتسجيل.
- markup متكرر: include داخل `reports/templates/reports/includes/`.
- سلوك متصفح متكرر: module مستقل داخل `reports/static/reports/js/`.

## قواعد التطوير

- ضع منطق الأعمال القابل لإعادة الاستخدام في خدمة، لا في القالب.
- لا تضف JavaScript داخليًا دون CSP nonce.
- لا تستخدم `|safe` لبيانات المستخدم؛ استخدم `json_script` أو JSON موثوقًا.
- أضف اختبار عزل لكل مسار يقرأ بيانات مدرسة أو مستخدم.
- شغّل CI المحلي الموضح في `README.md` قبل الدمج.
