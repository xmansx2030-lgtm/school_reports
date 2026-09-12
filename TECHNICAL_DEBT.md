# سجل الدين التقني

آخر مراجعة: 2026-09-12. لا يحتوي هذا السجل على أسرار أو بيانات إنتاج. كل بند
لم يُصلح في هذه المرحلة لأن إصلاحه يحتاج اختبارًا/قرارًا/تحققًا خارج المستودع،
لا لأنه مقبول دائمًا.

## P0 — حرج

لا توجد مشكلة P0 مؤكدة في لقطة المستودع الحالية.

## P1 — عالٍ

| المشكلة | الموقع | الأثر والمخاطر | التوصية ومعيار الإغلاق |
|---|---|---|---|
| أسرار قديمة في تاريخ Git | 19 commit تحتوي `.env`؛ راجع `SECURITY_SECRET_ROTATION_PLAN.md` | HEAD نظيف، لكن حذف الملف لا يبطل خمس فئات credentials ظهرت تاريخيًا | يثبت المسؤول البشري حالة تدوير كل مزود وسجلاته، ثم يقرر history rewrite منسقًا؛ ممنوع rewrite/force push تلقائيًا |
| ثلاث دورات اعتماد متبقية | SCC حول `reports.models/signals`، SCC في `model_parts.base`، وSCC billing/views/tasks | 24 module ما زالت تشارك في cycles، فتتسع آثار التعديل ويصعب startup isolation | تحويل مستهلك واحد في كل دفعة إلى imports مالك المجال مع characterization؛ المقياس القابل للتكرار هو `scripts/architecture_metrics.py` |
| import hubs و122 wildcard import | `reports/views/_helpers.py`, `reports/model_parts/base.py`, `reports/views/billing_*.py` | تخفي الاعتماديات، توسع أثر التعديل، وتمنع F401 من تحليل هذه الملفات فقط | نقل كل وحدة إلى imports صريحة على دفعات مع اختبارات المجال؛ تقييد compatibility surfaces بـ`__all__` قبل إزالة الاستثناء |
| 671 broad exception و62 handler صامتًا | خصوصًا `reports/model_parts/signals.py` ثم integration/view/task boundaries | قد يخفي فشل cache/notification أو فقد telemetry؛ بعضها fallback دفاعي مشروع | صنّف boundary بالاسم، أضف context آمنًا أو التقط النوع المحدد، واحذف S110/S112 suppression للملف عند اكتماله |

## P2 — متوسط

| المشكلة | الموقع | الأثر والمخاطر | التوصية ومعيار الإغلاق |
|---|---|---|---|
| ملفات Python كبيرة ومتعددة المسؤوليات | `reports/forms.py`, `reports/views/reports.py`, `reports/views/schools.py`, `reports/services_export.py`, `reports/tasks.py`, `config/settings.py` | مراجعة بطيئة وتعقيد مرتفع وتعارضات دمج | تقسيم مجال واحد كل مرة بعد characterization tests، مع واجهات توافق مؤقتة ومقياس حجم/تعقيد بعدي |
| قوالب ضخمة تحمل CSS/JS داخليًا | `my_subscription.html`, `admin_dashboard.html` وقوالب الإدارة الكبيرة | صعوبة اختبار الواجهة وتكرار السلوك/التنسيق | نقل السلوك إلى modules والأجزاء المتكررة إلى includes؛ فحص RTL و390x844 وdesktop وconsole بعد كل شريحة |
| دين CSS و`!important` مرتفع | `static/css/app.css`, `static/css/design-system.css` والقوالب | specificity غير متوقعة وتكلفة تعديل التصميم | جرد selectors فعلي عبر coverage بصري، توحيد tokens والمكوّنات، ثم حذف القواعد غير المستخدمة تدريجيًا |
| نطاق typecheck ما زال صغيرًا | mypy يفحص 6 public/core service files فقط | البوابة نظيفة فعليًا لكن أغلب Python غير typed تدريجيًا | وسّع ملفًا عالي القيمة كل دفعة؛ التالي permissions/auth ثم payment/integration، بلا global ignores |
| R2 والدفع وFCM لم تختبر عبر مزود شبكي | object storage/payment/notification integrations | mocks وPostgreSQL يثبتان العقد المحلي لا قبول credentials/signatures/provider restrictions | شغّل R2 sandbox وMoyasar/Tamara UAT وFirebase test project ببيانات غير إنتاجية؛ النتائج الحالية في `INTEGRATION_QA_REPORT.md` |
| رحلة مالك المنصة ليست مثبتة بصريًا بالكامل | browser smoke | رحلات المدير والـRTL والـviewports نجحت، لكن صفحات platform owner لم تكتمل بسبب سياسة URL للأداة | أعد smoke في runner/Browser يسمح بعنوان البيئة المعزولة، وسجل console/overflow دون لمس production |
| عدة خيارات stdin سرية في أمر الإعداد | `deploy/hetzner/apply_runtime_config.py` | جمع أكثر من `--*-from-stdin` في استدعاء واحد غير محدد لأن كل قارئ يستهلك stdin | منع الجمع في argparse أو اعتماد envelope JSON واضح؛ أضف اختبار CLI قبل تغيير العقد التشغيلي |

## P3 — منخفض

| المشكلة | الموقع | الأثر والمخاطر | التوصية ومعيار الإغلاق |
|---|---|---|---|
| أصول شعار مكررة بايتياً | web/PWA و`operations_mobile/assets/` | مساحة صغيرة وتحديث يدوي متعدد | أبقها ما دامت build roots مستقلة؛ إن أضيف build pipeline موحد، ولّد النسخ من أصل واحد |
| مفتاح Firebase Android عميل داخل `google-services.json` | `operations_mobile/android/app/google-services.json` | المفتاح عام بطبيعته لكن سوء قيود API/app يزيد إساءة الاستخدام | تحقق خارجيًا من Android package/SHA وAPI restrictions في Google Cloud/Firebase؛ لا تنقل service-account إلى التطبيق |
| retry per-device لإخفاق FCM المؤقت غير موجود | `operations/push.py` | 429/5xx قد يفقد notification؛ retry الساذج قد يكرر الإرسال للأجهزة التي نجحت | صمم delivery record/idempotency key ثم retry للأجهزة الفاشلة فقط، مع test لعدم التكرار |

## بنود أغلقت في هذه المرحلة

- PostgreSQL 16: migrations وconstraints وtransactions وJSON/timezone/locks
  وquery characterization والمدفوعات نجحت في خدمة معزولة.
- Redis 7.4 وCelery: cache/expiry/lock/failure، broker ping، queue routing ونتيجة
  task حقيقية نجحت.
- PDF: نجح إنشاء ملف صالح داخل صورة Linux؛ فشل Windows وحده قيد native host.
- قوالب البريد الأربعة المشتبه بها ثبتت Active عبر call sites وعقد render؛ لم
  تعد potential dead code.
- أُغلقت دورات task dispatch وassignment validation، وخُفضت SCC من 5 إلى 3.

## قواعد صيانة السجل

1. لا يغلق بندًا اختبار عابر؛ اذكر الدليل والـcommit الذي أزال سببه.
2. أي بند جديد يحدد موقعًا وأثرًا ومخاطرة وتوصية وأولوية.
3. لا تستخدم هذا الملف لتأجيل إصلاح آمن وصغير يقع ضمن التغيير الحالي.
4. راجع P1 قبل كل إصدار، وP2 في تخطيط كل دورة تطوير.
