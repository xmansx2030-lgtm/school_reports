# INTEGRATION QA REPORT

تاريخ التنفيذ: 2026-09-12. كل الخدمات محلية ومعزولة؛ لم تُستخدم قاعدة أو
credentials أو object أو عملية دفع إنتاجية.

## PostgreSQL 16

- شُغّلت كل migrations حتى `reports.0135` على container معزول.
- نجحت 5 عقود PostgreSQL الخاصة بـJSON/timezone، `select_for_update`، قيد
  uniqueness، rollback، ووجود indexes.
- نجحت 4 اختبارات query characterization في التشغيل المجمع مع Redis.
- نجحت 92/92 من عقود Moyasar/Tamara/discount/unified payment على PostgreSQL؛
  تشمل callback المكرر، mismatch، failure، abandoned payment، recovery
  وidempotent effects.
- `makemigrations --check --dry-run`: لا drift في القياس المرحلي.

## Redis / Celery

- Redis 7.4 معزول: نجحت 3 عقود serialization/expiry/atomic add، lock غير
  reentrant ثم قابل لإعادة الاكتساب، وgraceful connection failure.
- Celery worker حقيقي اتصل بالـbroker وأعاد `pong`.
- `cleanup_expired_sessions_task` وصلت عبر queue `periodic` ونفذت `SUCCESS`
  (`deleted=0`)؛ result backend حفظ واستعاد payload اختباريًا.
- انتهى worker بعد الفحص؛ لا تعتمد النتائج على eager mode وحده.

## Object storage (R2)

- 3 mock contract tests نجحت: المحافظة على ملف غير صوري وإعادة مؤشره، تمرير
  upload بعد media processing إلى S3 backend، ومنع parent path traversal.
- الروابط الخاصة مربوطة بسياسة signed URL لا تتجاوز 900 ثانية، ويحرسها اختبار
  إعدادات قائم.
- لم تتوفر bucket/credentials لـR2 Sandbox؛ upload/download/delete/signed URL
  عبر الشبكة ما زالت **EXTERNAL ACTION REQUIRED**.

## Payments

- 92/92 نجحت في Test/Mock على SQLite، ثم 92/92 على PostgreSQL 16 المعزول.
- لا توجد مفاتيح Moyasar/Tamara UAT في البيئة. قبول المزود، التوقيع الحقيقي،
  timeout الشبكي وduplicate callback الفعلي تبقى **EXTERNAL ACTION REQUIRED**.

## Firebase / notifications

- عقد FCM HTTP v1 يغطي غياب credentials، payload Android/channel، نجاح جهاز،
  `UNREGISTERED` وتعطيل token غير الصالح.
- أُغلق regression كان يبدّل متن الإشعار التالي بنص خطأ FCM.
- الإرسال يرسل HTTP v1 request منفصلًا لكل جهاز، لذلك لا يستخدم legacy batch
  API ولا يتجاوز batch-size contract. فشل 429/5xx لا يملك حاليًا retry
  per-device آمنًا من التكرار؛ بقي P3 موثقًا.
- لا تتوفر Firebase test project/service account؛ قبول FCM الفعلي وAndroid
  package/SHA/API restrictions تبقى **EXTERNAL ACTION REQUIRED**.

## Email fragments

القوالب الديناميكية الأربعة Active ومحمية بعقد resolution/render:

- `message.html`: data-rights، system probe، ومنصة البريد عبر Resend.
- `subscription_expiry.html`: تذكير انتهاء الاشتراك.
- `subscription_activated.html`: رسالة التفعيل بعد تطبيق أثر الدفع.
- `password_changed.html`: تنبيه تغيير كلمة المرور.

## PDF

- تنفيذ task على Windows وصل إلى worker الحقيقي لكنه تعذر بسبب غياب مكتبة
  `libgobject-2.0-0` الأصلية. هذا قيد host معروف، وليس نجاح PDF على Windows.
- داخل صورة Linux المبنية لهذه المرحلة نجح توليد دليل المستخدم كملف PDF صالح
  يبدأ بـ`%PDF-` وحجمه 1,466,850 بايت. استُخدم عنوان التطبيق المحلي داخل
  الحاوية، لذلك جُلبت الأصول دون أخطاء network؛ ظهرت فقط تحذيرات WeasyPrint
  المعروفة عن pseudo-selectors غير المدعومة.

## Docker / application smoke

- نجح build للصورة `school-reports:hardening-local` من context حجمه 3.45 MB؛
  حجم الصورة 335,989,473 بايت.
- يعمل التطبيق داخل الحاوية بالمستخدم غير الجذري `app` (`uid=10001`). لا تحمل
  الصورة `HEALTHCHECK` داخليًا؛ health checks معرفة في Compose، ولذلك يبقى
  تشغيل الصورة منفردة معتمدًا على فحص المشغل.
- نجحت migrations حتى `reports.0135` و`collectstatic` (279 ملفًا)، ثم boot عبر
  Gunicorn/Uvicorn.
- أعادت `/healthz/` حالة 200 مع DB/cache سليمتين، ونجحت أصول
  `/static/css/app.css` و`/static/manifest.json` و`/sw.js`، وصفحة `/login/`.
- لم يظهر في `/app` ملف `.env` أو SQLite أو مفتاح `.key`/`.pem`. كما لم تظهر
  build arguments سرية في history الصورة؛ هذا فحص محتوى مرئي وليس بديلًا عن
  registry scanner.

## Browser smoke

- فُحصت رحلة مدير مدرسة اصطناعيًا عند desktop `1440x900`، mobile `390x844`
  وtablet `768x1024`.
- نجحت صفحات login، dashboard، reports، notifications، إنشاء notification،
  assignments، meetings، subscription، complaints، ونسخة report المطبوعة.
- ظل `dir=rtl` صحيحًا ولم يظهر overflow أفقي موجب؛ نجح فتح قائمة الهاتف
  والتنقل، ونجح التبديل بين dark/light مع استمرار الحالة.
- لم تظهر console errors أو warnings في الرحلات التي فُحصت.
- لا تمثل هذه العينة Visual Coverage لكل Template. محاولة استكمال رحلة مالك
  المنصة بعد تسجيل الدخول توقفت بسياسة URL في أداة المتصفح؛ لم يُلتف على
  السياسة، وتبقى رحلة المالك الكاملة غير مثبتة بصريًا في هذه المرحلة.
