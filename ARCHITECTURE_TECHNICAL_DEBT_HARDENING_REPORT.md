# ARCHITECTURE & TECHNICAL DEBT HARDENING REPORT

تاريخ الإغلاق: 2026-09-12. هذه المرحلة Local/Sandbox فقط؛ لم يحدث push أو
deploy أو production write أو secret rotation أو history rewrite.

## Baseline

| Item | Value |
|---|---|
| Previous-report baseline | `9a45c5107fcc120dbf7ab99ac952187da889d087` |
| Actual phase starting HEAD | `d471e24be28d71ad529acf87c307ec7ff07f315f` |
| Ending implementation/evidence HEAD | `9494eb43d539fe14be718d5ee1c105e0600b185b` |
| `origin/main` at final fetch/current check | `27e09c061edcc0a87013d3fb4a2de1c2d9a31980` |
| Ahead / behind at ending implementation HEAD | `+23 / -0` |
| Phase commits through ending implementation HEAD | 18، إضافة إلى commit هذا التقرير فقط |
| Phase files changed | 64 (24 added, 40 modified) |
| Phase diff | 2,444 additions / 637 deletions |

الـ18 commits صغيرة ومحددة: reconciliation/audits، ثلاث دفعات task-boundary،
capacity وnotification services، mypy التدريجي، exception/startup visibility،
PostgreSQL/Redis/query contracts، storage/FCM، assignment cycle، form/view
decoupling، secret plan، ADR/onboarding، backend-neutral cache test، integration
evidence، ثم توسيع typecheck. لا يحتوي أي منها ملفات UI المتزامنة.

## Git Reconciliation

- أصبح ما في `origin/main` معلومًا، وكل تغييرات هذه المرحلة بقيت local ولم
  تُدفع أو تُنشر.
- بقيت 18 ملفات dirty تخص عمل UI متزامن: 11 template، اختبارا UX، وخمسة CSS.
  لم تُstage أو تُعدل أو تُنسب إلى commits هذه المرحلة.
- عند الإغلاق لا توجد staged files أو untracked outputs بعد commit التقرير؛
  الملفات الـ18 المذكورة هي dirty files المتبقية عمدًا.
- لم يستخدم `git add -A` أو reset/checkout أو حذف لتجميل status.

## Architecture

الاتجاه الموثق الآن:

```text
views / APIs / forms
        -> application services
        -> domain services / selectors
        -> models / integrations

producer -> neutral named-task dispatcher -> thin Celery task -> service
```

التحسينات الفعلية:

- `core.task_dispatch` فصل producers عن Celery task modules وحافظ على أسماء
  المهام وسياسة eager/fallback/trace.
- `reports.services_capacity` صار يملك حسابات الموارد والتنبيهات؛ task رقيقة.
- `reports.services_notifications` صار يملك materialization للمستلمين وقناة
  realtime الاختيارية؛ task رقيقة وباسمها العام نفسه.
- `reports.approval_errors` أزال ownership العكسي لأخطاء approval، وصار model
  يملك readiness validation.
- `reports.forms` لم يعد يستورد view helper للصلاحيات.
- ADRs تسجل task dispatch، typecheck التدريجي، وسياسة compatibility imports.

الدورات انخفضت من 5 SCC/35 module إلى 3 SCC/24 module. المتبقي موثق بالأسماء
في `ARCHITECTURE_DEBT_AUDIT.md`، لذلك لا يُدّعى إغلاق P1 المعماري بالكامل.

## God Files

عدد الأسطر هنا `splitlines()` على الملف الفعلي في Git، لا عدد الأسطر غير
الفارغة:

| File | Before | After | Action |
|---|---:|---:|---|
| `reports/tasks.py` | 1,994 | 1,782 | استخراج capacity وnotification business logic وإبقاء wrappers |
| `reports/utils.py` | 279 | 122 | نقل orchestration إلى dispatcher/service boundaries |
| `reports/forms.py` | 3,465 | 3,467 | فصل view dependency وdispatch فقط؛ بقي God file |
| `reports/services_export.py` | 2,439 | 2,439 | لم يُقسم؛ الدالة الأخطر تحتاج characterization إضافيًا |
| `reports/views/reports.py` | 2,815 | 2,815 | لم يُقسم بلا رحلة أضيق واختبارات نقل |

أضيف `reports/services_capacity.py` (208 سطرًا) و
`reports/services_notifications.py` (115 سطرًا) كوحدات domain واضحة. لا يوجد
split شكلي لبقية الملفات الكبيرة.

## Complexity

| Metric | Before | After |
|---|---:|---:|
| Circular SCCs | 5 | 3 |
| Modules in cycles | 35 | 24 |
| Ruff C901 findings | 34 | 32 |
| Broad `Exception` handlers | 686 | 671 |
| Syntactically silent broad handlers | 80 | 62 |
| Largest function | 587 lines | 587 lines |

أعلى دالة باقية هي `reports.services_export.build_school_export_zip_file`؛
لم تُفكك بلا حماية كافية لمراحل ZIP/workbook/storage المتعددة.

## Imports

- wildcard imports: 124 قبل، 122 بعد.
- SCCs: 5 قبل، 3 بعد.
- أُغلقت دورات operations task dispatch، file cleanup/generated export
  producers، وassignment validation.
- بقيت 122 wildcard في compatibility/model/view hubs؛ لم تُحذف آليًا لأن بعض
  الـpublic imports مستخدم خارجيًا وديناميكيًا. ADR-003 يحدد عقد الإزالة.

## Exception Handling

- broad exceptions: 686 → 671.
- silent broad handlers: 80 → 62.
- استُبدلت حالات startup/storage/API غير الآمنة بأنواع محددة أو telemetry مع
  context غير حساس.
- broad catch بقي فقط حين يكون boundary مقصودًا في الشرائح المعدلة؛ الدين
  المتبقي 671/62 مصنف P1 ولا يسمح بالحكم COMPLETE.

## CSS Debt

| Measurement | Count |
|---|---:|
| Committed baseline `!important` | 1,090 |
| Removed by this phase | 0 |
| Remaining in committed code | 1,090 |
| Current mixed worktree | 1,084 |

الفرق 6 يعود إلى CSS خارجي dirty وليس إلى هذه المرحلة. السبب الأكبر هو
dark-mode (168)، ثم `app.css` (159)، و`royal-theme.css` (139)، إضافة إلى
responsive/page-local overrides. أُنجز الجرد والتصنيف والخطة في
`docs/CSS_DEBT_AUDIT.md`، لكن التعديل أُجّل منعًا لتداخل الملكية. هذا سبب مستقل
لبقاء verdict جزئيًا.

## Templates

- ثبت أن email fragments الأربعة Active عبر call sites واختباري resolution و
  render؛ لم تُحذف بالتخمين.
- لم تُجزأ templates الكبيرة في هذه الدفعة لأن أعلى الملفات المطلوبة ضمن 11
  template dirty يملكها عمل UI متزامن.
- لم يُدّعَ coverage بصري لكل template.

## Type Safety

| Item | Result |
|---|---|
| Tool | mypy 2.3.1 |
| Files covered | 7 |
| Errors / warnings | 0 / 0 |
| Error-code suppressions in scope | 0 |
| `type: ignore` in scope | 0 |
| Untyped public functions in scope | 0 |

البوابة موجودة في CI وأمرها `python -m mypy`. النطاق يشمل core task/IP، task
names، generated-export/capacity/notification services. `Any` محصور في adapter
Django app registry داخل notification service لتجنب إعادة دورة model import.
النطاق التالي permissions/auth ثم payment/integrations.

## Database

- PostgreSQL 16 معزول: migrations حتى `reports.0135` نجحت.
- 5/5 عقود JSON/timezone، `select_for_update`، uniqueness، transaction rollback
  وindexes نجحت.
- 8/8 عقود query characterization نجحت، وتغطي directory/group dashboard،
  report selector، navigation لثلاثة أدوار، وstorage pressure.
- بوابة PostgreSQL/Redis/query النهائية: 16/16 في 59.454 ثانية.
- 92/92 عقود payment نجحت أيضًا على PostgreSQL.
- لا يوجد migration drift. لم تُستخدم production database ولم يُنفذ load test
  أو timing benchmark متعدد التكرار؛ query counts فقط هي البوابة.

## Redis / Celery

- Redis 7.4 معزول: 3/3 serialization/expiry/atomic-add/lock/failure contracts.
- worker حقيقي أجاب `pong`، ونُفذت `cleanup_expired_sessions_task` عبر queue
  `periodic` بحالة `SUCCESS`، ونجح result backend.
- task names/queues/retry/eager contracts بقيت ثابتة، والاختبارات تغطي enqueue
  failure وسياسة inline fallback وعدم ابتلاع الفشل.

## Object Storage

- 3/3 mock contracts: non-image rewind/preservation، S3 delegation، ورفض parent
  path traversal.
- signed URLs الخاصة محددة حتى 900 ثانية بعقد إعدادات.
- R2 network sandbox لم يتوفر: upload/download/delete/missing object/permission
  provider behavior هو **EXTERNAL ACTION REQUIRED**.

## Payments

- 92/92 على SQLite mock/test و92/92 على PostgreSQL 16 المعزول.
- غطت callback validation/replay، idempotency، mismatch، decline/abandonment،
  recovery، partial failure وtransaction effects دون تسريب أسرار.
- لم تتوفر Moyasar/Tamara UAT credentials؛ توقيع وقبول المزود الحقيقيان وtimeout
  الشبكي هما **EXTERNAL ACTION REQUIRED**. لم تُنفذ عملية دفع حقيقية.

## Firebase

- عقود FCM HTTP v1 تغطي غياب credentials، Android channel payload، success،
  `UNREGISTERED` وتنظيف token.
- أُصلح regression كان يستبدل متن الإشعار التالي بنص خطأ استجابة الجهاز السابق.
- لا يوجد Firebase test project؛ package/SHA/API restrictions والقبول الفعلي
  **EXTERNAL ACTION REQUIRED**. retry الآمن لـ429/5xx بقي P3.

## Security

| Gate | Result |
|---|---|
| Bandit high severity/high confidence | PASS |
| `pip-audit` على lock | PASS — no known vulnerabilities |
| Current tracked-secret guard | PASS |
| Django deploy check at WARNING | PASS |
| Environment/lock contracts | PASS — 230 env keys، 46 direct pins/97 locked packages |

HEAD لا يتتبع secret file. فحص history أثبت `.env` في 19 commit و17 snapshot
مع خمس فئات مفاتيح؛ لم تُطبع القيم. لا يوجد دليل مستودع يثبت أنها دُورت، لذلك
`SECURITY_SECRET_ROTATION_PLAN.md` يطلب تحقق المزود والمسؤول البشري قبل قرار
history rewrite. لم يحدث force push أو rewrite.

المراجعة اليدوية مدعومة باختبارات permissions/school isolation، upload/path،
export authorization، payment callback، notification endpoints وtrusted proxy
IP. scanners ليست الدليل الوحيد.

## Tests

- Full Django suite: **2,382 passed, 9 skipped, 0 failed** في 465.685 ثانية.
- skips هي integration/PDF Windows المشروطة؛ PostgreSQL/Redis/PDF Linux أغلقت
  ببوابات منفصلة كما سبق.
- targeted notification/task/cache بعد آخر تعديل: 28/28.
- PostgreSQL/Redis/query final gate: 16/16.
- payment: 92/92 SQLite ثم 92/92 PostgreSQL.
- أضيفت/وسعت عقود startup/API boundaries، task dispatch، capacity، notification،
  storage، FCM، email fragments، PostgreSQL، Redis، query growth وcompatibility.

## Lint and static validation

- Ruff: PASS.
- compileall: PASS.
- mypy (7 files): PASS.
- JavaScript syntax: 30/30 files.
- JSON/YAML/TOML/XML: 5/5/1/9 files.
- CSS linter غير مضبوط في المشروع؛ استخدمت collectstatic وbrowser smoke وجرد
  specificity بدل ادعاء gate غير موجودة.

## Builds

- Docker final image: PASS، `sha256:aea8879c...`, 336,003,552 bytes، والمستخدم
  `app` uid 10001.
- الصورة لا تعرف internal `HEALTHCHECK`; Compose يعرفه. هذا operational debt
  صغير عند تشغيل الصورة منفردة.
- Container smoke: migrations، boot، `/healthz/`، login، app CSS، manifest و
  service worker كلها PASS. لم يوجد `.env`/SQLite/`.pem`/`.key` تحت `/app`،
  ولم تظهر أسماء متغيرات حساسة في Docker history.
- Static build: 279 files copied و1,177 post-processed.
- PDF Linux: صالح ويبدأ `%PDF-`، 1,466,850 bytes. Windows host يفتقد GObject.
- Flutter: `pub get` PASS، `analyze` بلا issues، `test` 5/5، وdebug APK build
  PASS. لم تُحدّث dependencies القديمة عميانيًا ولم يُدعَ release APK.

## Browser QA

- Desktop 1440×900، mobile 390×844، tablet 768×1024.
- login، manager dashboard، reports، notifications/create، assignments،
  meetings، subscription، complaints وreport print نجحت.
- RTL، dark/light، mobile menu وnavigation نجحت؛ لا overflow أفقي موجب ولا
  console errors/warnings في الرحلات المفحوصة.
- رحلة platform owner الكاملة توقفت بسياسة URL للأداة بعد محاولة تسجيل الدخول؛
  لم يُلتف على السياسة. لذلك لا يوجد ادعاء Visual Coverage لكل template أو
  لكل admin journey.

## Documentation

أُنشئت أو حُدثت:

- `CURRENT_STATE_RECONCILIATION.md`
- `ARCHITECTURE_DEBT_AUDIT.md`
- `docs/CSS_DEBT_AUDIT.md`
- `TYPECHECK_BASELINE.md`
- `INTEGRATION_QA_REPORT.md`
- `SECURITY_SECRET_ROTATION_PLAN.md`
- `TECHNICAL_DEBT.md`
- `README.md`, `docs/ARCHITECTURE.md`, `docs/DEVELOPER_ONBOARDING.md`
- `docs/QUERY_CHARACTERIZATION.md` وثلاث ADRs.

## Technical Debt Remaining

### P0

لا توجد P0 مؤكدة.

### P1

- 3 SCC/24 module، منها model/signals، model-parts hub، وbilling/views/tasks.
- 122 wildcard imports في compatibility hubs.
- 671 broad handlers و62 handler صامتًا؛ بعضها boundary مشروع لكن الدين لم
  يُغلق ملفًا ملفًا.
- historical credential rotation/provider logs لم يثبتها مسؤول بشري.

### P2

- `forms.py`, `views/reports.py`, `services_export.py` وملفات إدارة كبيرة ما
  زالت God files.
- committed CSS ما زال يحوي 1,090 `!important`; لم يُخفض بسبب تعارض الملكية.
- mypy يغطي 7 ملفات فقط.
- R2/Moyasar/Tamara/Firebase network sandbox غير متاح.
- platform-owner visual journey وfull-template visual coverage غير مكتملتين.
- stdin contract في `deploy/hetzner/apply_runtime_config.py` يحتاج تصميم/اختبار
  قبل تغيير CLI.

### P3

- FCM 429/5xx per-device retry يحتاج idempotent delivery record.
- Firebase Android package/SHA/API restrictions تحتاج console verification.
- أصول الشعار المكررة بايتياً باقية لأن web/Flutter build roots مستقلة.

## HANDOVER READINESS

| Area | Score | Reason |
|---|---:|---|
| Code readability | 8.0/10 | خدمات جديدة واضحة، لكن God files باقية |
| Architecture clarity | 8.0/10 | map وADRs وحدود task موثقة؛ 3 SCC باقية |
| Testability | 9.0/10 | 2,382 test وعقود integrations/queries؛ live providers غائبة |
| Documentation | 9.5/10 | setup، architecture، debt، QA، ADRs وrotation موثقة |
| Maintainability | 8.0/10 | coupling/exception debt انخفض؛ CSS/hubs ما زالت مرتفعة |
| Type safety | 5.5/10 | بوابة فعلية نظيفة، لكن نطاقها 7 ملفات فقط |
| Operational clarity | 8.5/10 | Docker/Redis/Celery/PDF موثقة ومختبرة؛ provider ops خارجية |
| Developer onboarding | 9.0/10 | أوامر وتشغيل وخريطة إضافة feature واضحة |

## Code work versus external action

**CODE WORK: PARTIALLY COMPLETE.** أُغلقت شرائح عالية القيمة مع اختبارات، لكن
الدورات/import hubs/broad exceptions/CSS/God files المتبقية تمنع إعلان اكتمال
الكود نفسه، بصرف النظر عن القيود الخارجية.

**EXTERNAL ACTION REQUIRED:** إثبات تدوير الأسرار التاريخية، R2 sandbox،
Moyasar/Tamara UAT، Firebase test-project restrictions، وإعادة platform-owner
browser smoke في بيئة تسمح بها الأداة.

# FINAL VERDICT

## CODEBASE REFACTORING PARTIALLY COMPLETE

السبب: لا توجد P0 وكل البوابات المحلية المتاحة ناجحة، لكن شروط COMPLETE
الصريحة لم تتحقق: بقيت P1 cycles/import hubs/broad exceptions، لم ينخفض CSS
الملتزم، وبيئات provider الخارجية غير متاحة. الادعاء بـCOMPLETE سيكون مخالفًا
للأدلة رغم التحسن المعماري والاختباري الكبير.
