# ARCHITECTURE DEBT AUDIT

تاريخ خط الأساس: 2026-09-12. القياسات أدناه مأخوذة من `HEAD`
`d471e24be28d71ad529acf87c307ec7ff07f315f` قبل أي تعديل برمجي في مرحلة
Architecture & Technical Debt Hardening. استُبعدت الاختبارات والترحيلات من
تحليل اعتماد كود الإنتاج، بينما شمل فحص Ruff المشروع كما تضبطه إعداداته.

## Executive summary

لا توجد قرينة على P0 جديدة في هذه اللقطة، لكن حدود التطبيق ما زالت تتضمن
import hubs واسعة ودورات اعتماد تجعل أخطاء الاستيراد والتغيير المتسلسل أكثر
احتمالًا. أعلى شريحة P1 قابلة للإغلاق بأمان هي فصل نشر مهام Celery عن الخدمات
التي تستوردها تلك المهام. أما `models` و`views` compatibility surfaces فتحتاج
تفكيكًا تدريجيًا وعقودًا صريحة، لا حذفًا شاملًا.

## Baseline metrics

| Metric | Baseline |
|---|---:|
| Production Python files inspected | 270 |
| Circular strongly connected components | 5 |
| Modules participating in cycles | 35 |
| Wildcard import statements | 124 |
| Files containing wildcard imports | 54 |
| Ruff C901 findings (`max-complexity=20`) | 34 |
| Broad `Exception` handlers | 686 |
| Bare / `BaseException` handlers | 0 / 0 |
| Syntactically silent broad handlers | 80 |
| Largest Python module | `reports/forms.py` — 3,466 lines |
| Largest function | `build_school_export_zip_file` — 587 lines |

`Syntactically silent` يعني أن جسم الـhandler هو `pass` أو `continue` أو
`break` فقط. هذا جرد مراجعة وليس حكمًا آليًا بأن الحالات الثمانين كلها أخطاء؛
بعضها يقع في properties دفاعية أو optional instrumentation. أي إزالة تتطلب
فهم الـboundary واختبار السلوك.

## Result after the hardening slices

أُعيد القياس بالأداة القابلة للتكرار `scripts/architecture_metrics.py` بعد
آخر شريحة برمجية. أرقام baseline محفوظة من جرد بداية المرحلة؛ رقم ملفات
الإنتاج وحده لا يستخدم كمؤشر تحسن لأن تعريف الاستبعاد صُقل عند تحويل الجرد إلى
سكربت، أما مقاييس graph/import/AST فبقي نطاقها كود الإنتاج نفسه.

| Metric | Before | After | Change |
|---|---:|---:|---:|
| Circular strongly connected components | 5 | 3 | -2 |
| Modules participating in cycles | 35 | 24 | -11 |
| Wildcard import statements | 124 | 122 | -2 |
| Ruff C901 findings | 34 | 32 | -2 |
| Broad `Exception` handlers | 686 | 671 | -15 |
| Syntactically silent broad handlers | 80 | 62 | -18 |
| `reports/tasks.py` physical lines | 1,995 | 1,782 | -213 |
| Largest function lines | 587 | 587 | 0 |

ملفات الإنتاج التي يعرضها السكربت الحالي: 268. أكبر ملف ما زال
`reports/forms.py` (AST end line 3,467)، ولذلك لم يُدّعَ إغلاق دين God files
كله.

## Dependency map

الاتجاه المستهدف:

```text
views / APIs / forms
        |
        v
application services
        |
        v
domain services / selectors
        |
        v
models / repositories / integrations

Celery tasks -> thin orchestration -> application services
producers -> neutral task dispatcher -> Celery registry
```

العلاقات ذات الخطورة الحالية:

```text
operations.services -> operations.tasks -> operations.services
reports.file_cleanup -> reports.utils -> reports.tasks -> reports.file_cleanup
reports.views.* -> reports.views._helpers -> forms/models/tasks/views
reports.models <-> reports.model_parts.*
```

## Circular dependency components

1. `operations.services` ↔ `operations.tasks`.
   السبب: service ينشر `send_incident_push_task` باستيراد task محلي، بينما task
   يستورد service للتنسيق. المالك الصحيح للنشر هو dispatcher محايد والـtask
   يبقى thin orchestrator.
2. دورة `reports` الكبرى (20 وحدة): `file_cleanup`, `forms`, generated-export
   modules, PDF modules, realtime, export services, `tasks`, Telegram, `utils`,
   billing view hubs وweb push. السبب الأهم هو أن producers تستورد task modules
   أو utility hubs واسعة، وbilling compatibility imports تعيد تصدير وحدات أخرى.
3. `reports.models` / `reports.model_parts` / `reports.ai_usage`.
   السبب: `reports.models` واجهة توافق تجمع model parts، وبعض الأجزاء تعود إلى
   الواجهة المجمعة. يحتاج هذا إلى عقود model imports ثم تحويل المستهلكين إلى
   الجزء المالك تدريجيًا.
4. ثماني وحدات من `reports.model_parts` مرتبطة عبر wildcard hub في
   `model_parts.base`. هذا الملف يجمع Django symbols وconstants/helpers، ولذلك
   أصبح dependency surface غير معلن.
5. `reports.model_parts.assignments` ↔ `reports.services_assignments`.
   السبب يحتاج characterization مستقل لمسار حالة التكليف قبل الفصل.

## Wildcard import hubs

أكبر targets المقاسة:

| Hub | Importers |
|---|---:|
| `reports.views._helpers` | 26 |
| `reports.model_parts.base` | 23 |
| `reports.views.billing_core` | 4 |

بقية الـwildcards تشمل compatibility re-exports في `reports.views`,
`reports.model_parts` وواجهات domains داخل `views`. الأولوية ليست تقليل الرقم
شكليًا؛ الأولوية إزالة الاستيراد النجمي من implementation modules، ثم تقييد
واجهات التوافق بـ`__all__` واختبار contract قبل أي كسر.

## God files and complexity

| File | Lines | Functions | Classes | Primary risk |
|---|---:|---:|---:|---|
| `reports/forms.py` | 3,466 | 97 | 70 | notification form تجمع validation وdispatch |
| `reports/views/reports.py` | 2,816 | 43 | 0 | querying, rendering, exports and permissions |
| `reports/mansour_assistant.py` | 2,656 | 59 | 2 | intent routing and provider/fallback behavior |
| `reports/views/schools.py` | 2,516 | 53 | 6 | school administration journeys |
| `reports/services_export.py` | 2,440 | 39 | 0 | workbook, archive index and ZIP generation |
| `reports/views/billing_platform.py` | 2,396 | 39 | 1 | admin UI plus payment/subscription orchestration |
| `reports/tasks.py` | 1,995 | 40 | 0 | multiple queues and operational orchestration |

أعلى C901 هو `build_school_export_zip_file` (63)، ثم
`achievement_file_detail` (60)، ثم `NotificationCreateForm.save` (56)، ثم PDF
fallback وreport printing (50/48). تُقدّم الدوال ذات اختبار جيد وحدود مستقلة؛
لا يُجزأ الملف لمجرد خفض عدد الأسطر.

## Broad exception classification

- **Legitimate boundaries:** Celery/task top-levels، provider/storage/network
  probes، best-effort telemetry، وCLI commands. يجب أن تسجل context آمنًا أو
  تعيد الخطأ بصياغة domain واضحة.
- **Fallback boundaries requiring review:** PDF fallbacks، optional notification
  channels، settings parsing، وإظهار metadata. يجب أن يظل fallback ظاهرًا في
  logs/metrics.
- **Unsafe candidates:** 80 handler صامتة نحويًا. تراجع أولًا الحالات الواقعة
  في services/views/tasks التي قد تخفي فقد بيانات؛ properties الدفاعية تأتي
  بعدها.

لا توجد bare exceptions أو `BaseException` catches في النطاق، وهذا يمنع التقاط
إشارات الإنهاء عن طريق الخطأ.

## Work order and safety

1. **P1:** نقل task dispatch إلى `core`، وإزالة دورة `operations`، وفصل
   `file_cleanup` عن `utils` مع إبقاء compatibility exports واختبارات أسماء
   المهام/fallback.
2. **P1:** إضافة contracts للـwildcard hubs ثم تحويل implementation modules
   تدريجيًا إلى explicit imports.
3. **P1/P2:** تحويل capacity monitoring إلى application service وإبقاء Celery
   task رقيقة، ما يخفض C901 ويحافظ على الاسم والـqueue/eager behavior.
4. **P2:** characterization لمسارات queries/exports قبل تحسينها.
5. **P2:** typecheck تدريجي للنواة وحدود الخدمات الجديدة، بلا global ignores.
6. **P2/P3:** templates/CSS only بعد عزل تغييرات الواجهة الحالية وتوفير browser
   regression؛ لا تداخل مع الـ18 ملفًا المنسوبة للعمل الخارجي.

تعاد هذه القياسات في التقرير النهائي من نفس النطاق. أي انخفاض نتج عن dirty UI
work يُعرض منفصلًا ولا ينسب إلى commits هذه المرحلة.

## Implemented boundaries

- `core.task_dispatch` صار boundary محايدًا لنشر المهام بالاسم؛ أُغلقت دورة
  `operations.services ↔ operations.tasks` ودورات file cleanup/generated export
  الناتجة عن استيراد task module من المنتج.
- نُقل حساب capacity والتنبيه إلى `reports.services_capacity` وبقيت مهمة
  Celery thin orchestrator مع الاسم والـqueue والـretry contract الحالي.
- نُقل إنشاء الإشعار وتحديد المستلمين والقنوات إلى
  `reports.services_notifications`؛ بقي `send_notification_task` غلاف تنسيق.
- أصبحت أخطاء اعتماد التكليف مملوكة لـ`reports.approval_errors`، وصار model
  يملك readiness validation بدل الدوران إلى application service.
- لم تعد `reports.forms` تستورد view helper للتحقق من مدير المدرسة؛ تستخدم
  canonical permission helper مباشرةً.

## Remaining measured cycles

يعرض السكربت حاليًا ثلاث SCCs، ولا يجوز اعتبارها مغلقة:

1. تسع وحدات حول `reports.models`, `permissions`, `signals`, cache/realtime وweb
   push. مركزها واجهة model compatibility والـsignals.
2. ثماني وحدات model parts حول wildcard hub في `reports.model_parts.base`.
3. سبع وحدات billing/views مع `reports.tasks` و`reports.views._helpers`.

هذه P1 معمارية متبقية. تفكيكها يحتاج تحويل المستهلكين إلى imports صريحة وعقود
public compatibility على دفعات؛ حذف الـhubs دفعة واحدة غير آمن.
