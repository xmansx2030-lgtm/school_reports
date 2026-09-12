# CODEBASE REFACTORING & MAINTAINABILITY REPORT

تاريخ الإغلاق: 2026-09-12. نقطة الأساس النظيفة:
`9a45c5107fcc120dbf7ab99ac952187da889d087`.

## Scope and evidence

تم جرد كل ملف متتبع في Git، ثم تطبيق فحص مناسب لنوعه: AST وlint وcompile
لـPython، اكتشاف وتشغيل اختبارات Django، parsing لـJavaScript وملفات الإعداد،
فحص الصور، مقارنة التجزئات، فحص الاعتماديات والأسرار، بناء Docker، وتحليل وبناء
تطبيق Flutter. راجعت المسارات الحرجة يدويًا، وشملت المصادقة والصلاحيات وعزل
المدارس والدفع والتصدير والإشعارات والتشغيل والنشر. يحتوي
`docs/CODEBASE_INVENTORY.md` على السجل المصنف ملفًا بملف.

لا يعني الجرد أن كل ملف مولّد أو مورّد أُعيدت كتابته: migrations محفوظة كسجل
schema، وvendor assets محفوظة كما وردت، والأصول الثنائية فُحصت بحسب نوعها.

## Files Reviewed

- 1,060 ملفًا متتبعًا عند خط الأساس، دون استثناء من الجرد.
- 1,068 ملفًا متتبعًا في النتيجة الحالية، بعد إضافة أدوات الجرد ووثائق التسليم
  وحدود dispatch وخدمة التصدير.
- 243,931 سطرًا نصيًا عند خط الأساس، منها 146,731 سطر Python و81,973 سطر
  templates.
- 572 ملف Python، و240 template مصنفًا، و162 ملف اختبار، و152 migration، و63
  ملف static source في خط الأساس.

## Files Modified

- 76 ملفًا قائمًا عُدّل.
- 10 ملفات جديدة أضيفت.
- ملفان حُذفا.
- إجمالي سطح التغيير الملتزم مقارنة بخط الأساس: 88 ملفًا، و2,996 سطرًا مضافًا
  و1,149 سطرًا محذوفًا عبر 17 commit. لا توجد migration أو schema أو endpoint
  أو response contract جديدة.

## Files Removed

- `.codex-repair/post_reboot_verify.ps1`: أثر إصلاح جهاز Windows لا ينتمي
  للمنتج ولا يملك أي reference.
- `_dark_extracted.css.part`: ناتج استخراج مؤقت غير مستخدم؛ القواعد الفعلية
  موجودة في ملفات CSS المتتبعة.

## Dead Code Removed

- حُذف المسار القديم `_legacy_bulk_import_teachers`؛ الـroute الحالية تستخدم
  تدفق `teacher_onboarding` المحمي بالاختبارات.
- حُذفت 8 دوال خاصة أخرى بلا import أو call أو route أو test reference، وخمس
  دوال داخلية لم يعد لها وجود بعد حذف التدفق القديم.
- بلغ تنظيف شريحة dead code وحدها 634 سطرًا محذوفًا مقابل 6 أسطر بديلة.
- بقيت أربعة email fragments ضمن `POTENTIAL DEAD CODE` لأن الاسم قد يأتي من
  قاعدة البيانات أو المزود ديناميكيًا؛ حذفها دون telemetry مخاطرة غير مقبولة.

## Duplicate Code Removed

حُذف 16 تعريفًا محليًا مكررًا واستُبدل بست واجهات مركزية واضحة:

- حارسا platform superuser وexecutive-director groups وactive school في
  `reports/view_access.py`.
- إنشاء أقسام ملف الإنجاز في `reports/services_achievement.py`.
- استخلاص IP للـaudit والـrate limiting في `core/client_ip.py`.

احتُفظ بنسخ الشعارات المتطابقة بين web وFlutter لأنها مدخلات build مستقلة،
ولأن abstraction لها يضيف coupling مقابل توفير ضئيل.

## Large Files Split

تم تقسيم الجزء التشغيلي الآمن الذي كانت له اختبارات مباشرة:

- قبل: `deploy/hetzner/apply_runtime_config.py::_collect` — 163 سطرًا ومسؤوليات
  إعداد عدة مزودين.
- بعد: `_collect` — 14 سطرًا، وثماني دوال جمع متخصصة بين 7 و31 سطرًا.
- الملف الكلي: 545 → 567 سطرًا بسبب الأسماء والعقود الواضحة؛ أكبر دالة أصبحت
  61 سطرًا، واختفى تحذير C901 عن `_collect`.
- انخفضت قائمة C901 الاستكشافية عند عتبة 20 من 36 إلى 34 دالة؛ البقية مسجلة
  كدين تقسيم وليست جزءًا من بوابة lint الإلزامية الحالية.
- قبل: `reports/tasks.py` — 2,164 سطرًا، ويتضمن تنفيذ بناء التصدير كاملًا.
- بعد: `reports/tasks.py` — 1,994 سطرًا، ومهمة التصدير غلاف رقيق؛ انتقل منطقها
  إلى `reports/services_generated_exports.py` في 243 سطرًا مركزة على المجال.

لم تُقسّم ميكانيكيًا ملفات forms/exports/reports/templates الضخمة؛ عقودها
المتشعبة تحتاج characterization/query/browser budgets لكل مجال. أُدرجت بأولوية
P2 في `TECHNICAL_DEBT.md` بدل تنفيذ big-bang rewrite يخالف شرط عدم الـregression.

## Architecture Improvements

- أضيفت حدود مشتركة لصلاحيات views وهوية IP بدل نسخها عبر الوحدات.
- أصبح جرد المشروع deterministic وقابلًا لإعادة التوليد بالأمر
  `python scripts/codebase_inventory.py --output docs/CODEBASE_INVENTORY.md`.
- وُثقت نقاط الدخول والطبقات والمسارات الحرجة والمعاملات وidempotency ومكان
  إضافة كل نوع من الميزات في `docs/ARCHITECTURE.md`.
- بدأ تفكيك دورات import دون كسر public surfaces: أُغلق الاعتماد العكسي من
  `file_cleanup.py` و`generated_exports.py` إلى `tasks.py`، وبقيت المسارات
  الأخرى وواجهات wildcard التوافقية معلنة كدين P1.

## Naming Improvements

- تحمل helpers المركزية أسماء domain صريحة بدل نسخ محلية غامضة.
- سُمّيت مراحل إعداد runtime بحسب المزود والغرض بدل دالة جمع واحدة.
- أُزيلت أسماء/متغيرات محلية غير مستخدمة، مع إبقاء أسماء public API ثابتة.

## Dependency Cleanup

- روجعت المتطلبات المباشرة الـ46 مقابل lock يحوي 97 حزمة مثبتة ومجزأة hashes.
- كشف الفحص النهائي ثغرة منشورة حديثًا في WeasyPrint 69.0
  (`PYSEC-2026-3940` / `CVE-2026-55073`)؛ رُقّيت إلى 70.0 وجُدد lock دون تحديث
  شامل أعمى.
- `pip-audit`: لا توجد ثغرات معروفة بعد التحديث.
- لم تُحذف حزمة اعتمادًا على import scan وحده لأن plugins وDjango backends
  وCLI entry points قد تُحمّل ديناميكيًا.

## Security Improvements

- لم تعد سجلات back-office تثق بـ`X-Forwarded-For` الذي يرسله أي عميل؛ العنوان
  المباشر هو الأصل، ولا يُقبل `X-Real-IP` إلا من proxy موثوق مضبوط.
- لا يسجل Celery نص الطلب كاملًا عند فشل JSON؛ بقي السياق المفيد دون payload
  قد يحوي بيانات شخصية أو tokens.
- نجح فحص الأسرار الحالية وBandit عالي الشدة/الثقة.
- لا توجد أسرار خادم متتبعة في الرأس الحالي. أسرار `.env` التاريخية تظل P1
  خارجيًا: يلزم إثبات تدويرها وإدارة إعادة كتابة التاريخ خارج هذا refactor.

## Performance Improvements

- استبعاد `operations_mobile` ومخرجاته من Docker context منع إدخال مخرجات
  Flutter/Gradle المحلية التي تجاوزت عدة غيغابايت؛ للعميل pipeline مستقل.
- أضيف cache قابل للكتابة للخطوط تحت المستخدم غير الجذري، فأصبح إنشاء PDF
  داخل الحاوية بلا تحذيرات Fontconfig المتكررة.
- لم تُغيّر ORM queries الحرجة بلا query-count characterization؛ مخاطر N+1
  المتبقية في dashboards/exports موثقة.

## Tests Added

- characterization tests لرفض forwarded IP غير الموثوق، وللفصل بين audit IP
  وfallback الخاص بالـrate limiting.
- assertions في اختبارات complaints وplatform email تثبت قيمة IP المسجلة.
- ظلّت تغطية الصلاحيات والدفع والعزل والتصدير والإشعارات والعقود الحالية هي
  شبكة الأمان الأساسية لكل شريحة refactor.

## Test Results

- خط الأساس: 2,343 اختبار Django ناجح، 1 skipped، خلال 417.833 ثانية.
- الاختبار الشامل الوسيط على شجرة العمل الحالية: 2,350 اختبارًا ناجحًا،
  1 skipped، خلال 296.158 ثانية.
- الاختبار النهائي بعد تقوية حدود المهام والتصدير: 2,356 اختبارًا ناجحًا،
  1 skipped، خلال 503.453 ثانية.
- شريحة trusted IP والصلاحيات والخدمات المتأثرة: 134 اختبارًا ناجحًا.
- إعداد runtime بعد التقسيم: 17 اختبارًا ناجحًا.
- PDF/invoice regression على Windows: 20 ناجحًا و1 skipped بسبب native library.
- Flutter: 5 اختبارات ناجحة.
- اختبار Linux الحقيقي: WeasyPrint 70.0 أنشأ PDF صالحًا داخل صورة Docker.

## Lint

- `ruff check .`: ناجح.
- فُعّلت F401 وF841 على مستوى المشروع وحُصرت استثناءات F401 في ستة import hubs
  توافقية معروفة فقط.
- `compileall`: ناجح.
- JavaScript syntax وJSON/YAML/TOML/XML parsing وshell syntax: ناجحة.
- 50 صورة raster اجتازت التحقق.

## Typecheck

لا توجد بوابة mypy/pyright قائمة في المشروع، لذلك لا يصح ادعاء نجاح typecheck
شامل. حافظ التغيير على type hints المفيدة وأضافها للواجهات العامة الجديدة،
وسُجل إدخال typecheck تدريجيًا كدين P2.

## Build

- `collectstatic --clear` بإعداد production: 279 ملفًا نُسخ و1,177 ملفًا
  عولج post-process بنجاح.
- `flutter build apk --debug`: ناجح، وأنتج `app-debug.apk`.
- `docker build --tag school-reports:refactor-qa .`: ناجح.
- image smoke: يعمل التطبيق داخل الحاوية بالـUID 10001، ونجح Django check،
  وإنشاء PDF صالح بـWeasyPrint 70.0.

## Configuration and migrations

- توثق `.env.example` جميع مفاتيح البيئة الحرفية الـ230 المستعملة في Python
  دون أسرار، ويمنع `scripts/check_env_example.py` وCI أي drift جديد.
- `manage.py check` و`check --deploy --fail-level WARNING`: ناجحان بإعدادات
  sandbox صريحة.
- `makemigrations --check --dry-run`: لا يوجد schema drift.
- لم تُنشأ migration في هذه المرحلة.

## Documentation

- `README.md`: overview، المتطلبات، الإعداد، environment/database، التشغيل،
  الاختبار، build، النشر، بنية المجلدات، والخطوات التشخيصية.
- `docs/ARCHITECTURE.md`: boundaries وcritical journeys وtransactions وjobs
  وexternal integrations ومسار إضافة الميزات.
- `CODEBASE_AUDIT_REPORT.md`: لقطة القياس والمخاطر قبل التغيير.
- `docs/CODEBASE_INVENTORY.md`: تصنيف كل ملف متتبع.
- `TECHNICAL_DEBT.md`: ما لم يمكن تغييره بأمان الآن، مع location/impact/risk/
  recommendation/priority.

## Browser and UX QA

نجحت رحلة دخول حقيقية ببيانات اصطناعية، ثم dashboard وemail وcomplaints وmeetings
وassignments. عند 390×844 ظل المستند RTL بلا overflow أفقي، ونجح dark mode
بقيمة computed داكنة وخط مقروء، ولم تظهر console errors أو warnings. تكرر فحص
dashboard عند 1440×900 بلا overflow أو أخطاء console. هذا دليل محلي على الرحلات
المفحوصة، وليس ادعاء تغطية بصرية لكل template.

بعد ظهور تغييرات الواجهة المتزامنة أُعيد الفحص على النسخة الحالية: صفحة
الإشعارات عند 390×844 و1440×900 بقيت RTL دون overflow للمستند أو أخطاء console،
وطبقة رسائل النظام كانت fixed أسفل الترويسة المحسوبة (80px على الجوال و84px
على سطح المكتب) مع `pointer-events: none` للحاوية. أعيد كذلك بناء صورة Docker
من شجرة العمل الحالية.

## Technical Debt Remaining

- P1: تدوير أسرار Git التاريخية وإثباته خارجيًا؛ إزالة wildcard import hubs؛
  استكمال فك import cycles بعد إغلاق مساري تنظيف الملفات والتصدير؛ وتنظيف
  قائمة broad-exception المتبقية ملفًا ملفًا.
- P2: تقسيم god files والقوالب، تقليل 1,090 استخدامًا لـ`!important` مع visual
  regression coverage، إضافة typecheck تدريجي، وتشغيل تكامل PostgreSQL/Redis/R2
  ومزودي الدفع المعزول.
- P3: إثبات الاستخدام الديناميكي لقوالب البريد وفحص قيود Firebase Android API.

## Handover Readiness

| المجال | التقييم | المبرر |
|---|---:|---|
| Code readability | 8/10 | dead code/imports والتكرار المؤكد انخفض؛ god files باقية ومعلنة |
| Architecture clarity | 8/10 | خريطة وحدود ومسارات حرجة موثقة؛ دورتان مباشرتان أُغلقتا ومسارات أخرى باقية |
| Testability | 9/10 | أكثر من 2.3k اختبار وcharacterization tests وبوابات مستقلة |
| Documentation | 9/10 | setup/architecture/inventory/audit/debt متاحة من الجذر |
| Maintainability | 8/10 | lint/env/dependency/build contracts أقوى؛ CSS/type debt باقٍ |
| Developer onboarding | 9/10 | أوامر البدء والاختبار والبناء والنشر ومكان business logic موثقة |

يمكن لمطور جديد تشغيل المشروع وتتبع business logic والـAPIs والـjobs ومسار
النشر خلال ساعات. يلزمه الرجوع إلى سجل الدين قبل لمس import hubs أو الملفات
الضخمة، وإلى اختبارات العقود قبل أي تغيير في الدفع أو الصلاحيات.

## Architecture hardening follow-up — 2026-09-12

- `74ad0e1b`: فصل جدولة إعادة محاولة حذف ملفات التخزين عن استيراد
  `reports.tasks`، مع إبقاء fallback المتزامن عند تعطل broker وتثبيت اسم المهمة.
- `780d89d2`: نقل بناء التصدير من مهمة Celery ضخمة إلى
  `services_generated_exports.py`، وإضافة حد dispatch بالاسم يحافظ على eager
  mode، والـqueue المسماة `images`، وعقد اسم المهمة.
- انخفض `reports/tasks.py` من 2,164 إلى 1,994 سطرًا. أصبحت مهمة التصدير غلافًا
  رقيقًا، والخدمة المركزة الجديدة 243 سطرًا، وملف dispatch العام 34 سطرًا.
- أضيفت ستة اختبارات characterization/contract لمساري retry والdispatch وأسماء
  المهام والاسترداد، ونجحت المجموعة المركزة 27/27.
- نجح الاختبار الشامل الحالي: 2,356 اختبارًا، حالة واحدة skipped، خلال
  503.453 ثانية. كما نجحت Ruff وcompileall وDjango check وmigration drift و
  env/lock contracts وBandit وpip-audit.
- نجح `check --deploy` بإعداد sandbox صريح، و`collectstatic` (279 ملفًا و1,177
  post-processed)، وبناء `school-reports:architecture-refactor-qa`. أكد Linux
  smoke العمل بالـUID 10001 وثبات اسمي مهمتي التنظيف والتصدير وقابلية استيراد
  الخدمة الجديدة.
- بقيت استيرادات مهمة أخرى داخل forms/signals/views وnotification integrations؛
  لذلك يظل الحكم العام `PARTIALLY COMPLETE` ولا تُسوّى هذه المسارات بلا
  characterization tests خاصة بكل مجال.

## Final QA boundary

كل أدلة QA هنا محلية ومعزولة، ولم أنفذ push أو migration أو كتابة على
production ضمن هذه المهمة. عند الإغلاق كان `origin/main` قد تحرك خارجيًا حتى
`27e09c06` ويحتوي commits التنفيذ، بينما بقي commit التقرير محليًا ahead بواحد؛
هذا رصد لحالة Git لا دليل نشر حي. لا يُدّعى أن نجاح الصورة المحلية يثبت نشرًا
على production.

أثناء توقف هذه المهمة ظهرت تغييرات متزامنة غير تابعة لهذا refactor في 18 ملف
واجهة واختبار، بتاريخ 2026-09-12 بين 14:45 و14:49. حُفظت دون تعديل أو commit
ضمن هذه السلسلة، لكن الاختبار النهائي وRuff شُملاها ونجحا. لذلك تبقى شجرة
العمل dirty بعد إغلاق commits الخاصة بهذه المهمة؛ وشملها أيضًا collectstatic
وبناء Docker وفحص المتصفح الحالي. لا يجوز نسب تلك الملفات إلى هذا التقرير أو
حذفها لأجل الحصول على `git status` نظيف.

# FINAL VERDICT

## CODEBASE REFACTORING PARTIALLY COMPLETE

المرحلة المنفذة تحقق تنظيفًا واسعًا وآمنًا، جردًا كاملًا، بوابات جودة أقوى،
توثيق تسليم، وبناءات واختبارات فعلية دون كسر العقد العامة. لا يمكن وصفها
`COMPLETE` بصدق ما دامت ملفات ضخمة ودورات import ودين CSS/typecheck وتحقق
التكاملات الخارجية باقية. محاولة إغلاقها دفعة واحدة كانت ستخالف القاعدة
الذهبية وشرط منع الـBig Bang rewrite؛ لذلك بقيت محددة وقابلة للتنفيذ في سجل
الدين بدل إخفائها.
