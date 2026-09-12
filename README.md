# منصة توثيق

منصة Django عربية متعددة المدارس لإدارة التقارير، والتكليفات، والتذاكر،
والتعاميم والتوقيعات، وملفات الإنجاز، والاشتراكات والمدفوعات والأرشفة. الواجهة
الأساسية RTL/PWA، ويوجد تطبيق Flutter مستقل لمراقبة التشغيل.

## المتطلبات

- Python 3.12 وGit.
- SQLite للتطوير البسيط، أو PostgreSQL عند اختبار بيئة شبيهة بالإنتاج.
- Redis اختياري محليًا، ومطلوب في الإنتاج للكاش والجلسات وChannels وCelery.
- Docker عند اختبار صورة الإنتاج أو إنشاء PDF على Windows دون تثبيت GTK/Pango.
- Flutter SDK فقط عند تطوير `operations_mobile/`.

## بداية سريعة على Windows

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
python manage.py migrate
python manage.py runserver
```

الإعداد الافتراضي يستخدم SQLite، وكاش الذاكرة، وبريد console. اترك Redis وR2
ومفاتيح مزودي الدفع فارغة ما لم تكن تختبر تكاملها تحديدًا. لا تضع أي secret
حقيقي في `.env.example` أو Git.

## أوامر التطوير والتحقق

```powershell
# صحة Django وعدم وجود تغيّر غير مهاجر
python manage.py check
python manage.py makemigrations --check --dry-run

# التحليل الساكن وعقد البيئة وقفل الاعتماديات
ruff check .
python -m compileall -q config core reports maintenance operations scripts deploy manage.py
python scripts/check_env_example.py
python scripts/check_requirements_lock.py

# الاختبارات
$env:DJANGO_SETTINGS_MODULE = "config.test_settings"
python manage.py test --verbosity 1

# صورة الإنتاج
docker build --tag school-reports:local .
```

لتطبيق العمليات، شغّل الأوامر بالتسلسل من داخل `operations_mobile/`:

```powershell
flutter pub get
flutter analyze
flutter test
flutter build apk --debug
```

## بنية المشروع

```text
config/                 إعدادات Django وASGI وCelery والمسارات
core/                   health/readiness، القياسات، الحماية وmiddleware
reports/model_parts/    نماذج المجال المقسمة؛ reports/models.py واجهة توافق
reports/views/          عروض HTTP بحسب المجال
reports/services_*.py   منطق أعمال قابل للاختبار وإعادة الاستخدام
reports/templates/      قوالب Django RTL ومكوّناتها الجزئية
reports/static/         CSS وJavaScript وPWA/service worker
reports/tests/          اختبارات المجالات والرحلات وعزل المدارس
maintenance/            معاينة وتنفيذ صيانة السنة الدراسية
operations/             API مراقبة التشغيل والإشعارات الأصلية
operations_mobile/      تطبيق Flutter للمراقبة
deploy/hetzner/         Compose وCaddy وأدوات إعداد/نشر الخادم
scripts/                فحوص المستودع وأدوات التشغيل الآمنة
docs/                   المعمارية والتشغيل والتسليم والتقارير
```

راجع [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) لتدفق الطلب وحدود المجالات
ومواضع منطق الأعمال، و[CODEBASE_AUDIT_REPORT.md](CODEBASE_AUDIT_REPORT.md)
لخط الأساس التفصيلي، و[TECHNICAL_DEBT.md](TECHNICAL_DEBT.md) للدين المتبقي.
مسار التسليم العملي موجود في
[docs/DEVELOPER_ONBOARDING.md](docs/DEVELOPER_ONBOARDING.md)، ونتائج الخدمات
المعزولة في [INTEGRATION_QA_REPORT.md](INTEGRATION_QA_REPORT.md).

## قاعدة البيانات والهجرات

- `DATABASE_URL` هو المسار المفضل لـ PostgreSQL؛ إعدادات `DB_*` توافقية.
- لا تعدّل migration منشورًا. غيّر النموذج، ثم نفّذ `makemigrations` وراجع SQL
  والأثر، وبعدها شغّل الاختبارات و`migrate`.
- كل استعلام متعدد المستأجرين يجب أن يُقيّد بمدرسة موثوقة من
  `request.active_school`/`SchoolMembership`، لا بمعرّف من العميل وحده.
- استخدم transaction للخطوات المالية أو متعددة الكتابة، واجعل callbacks
  والمهام القابلة للتكرار idempotent.

## الخدمات الخارجية

- PostgreSQL وRedis.
- Cloudflare R2 للوسائط الخاصة والروابط الموقعة.
- Celery/Beat وDjango Channels.
- Moyasar وTamara للدفع، وResend للبريد، وWeb Push/FCM للإشعارات.
- OpenAI لميزات الذكاء الاصطناعي الاختيارية، وSentry/Telegram للرصد.

جميع المتغيرات الحرفية التي يقرأها كود Python موثقة في `.env.example`، بينما
قالب الخادم الآمن موجود في `deploy/hetzner/env.production.example`. يتحقق
`scripts/check_env_example.py` آليًا من عدم ظهور مفتاح جديد بلا توثيق.

## الأمان والعقود العامة

- لا ترفع `.env` أو قاعدة SQLite أو `media/` أو مفاتيح Firebase الخدمية.
- وسائط R2 خاصة افتراضيًا؛ لا تفعل `MEDIA_PUBLIC_ACCESS_ENABLED` دون مراجعة
  خصوصية مستقلة.
- لوحة المنصة وواجهات المدارس لها حراس منفصلون. أعد استخدام helpers الصلاحيات
  ولا تنشئ فحص دور محليًا داخل كل view.
- حافظ على endpoints وبنية JSON وأسماء الحقول المستخدمة من PWA وFlutter
  والتكاملات الخارجية.
- كل JavaScript داخلي في القوالب يحتاج `nonce="{{ CSP_NONCE }}"`، ولا تستخدم
  `|safe` لبيانات مستخدم.

## PDF على Windows

WeasyPrint يحتاج مكتبات Pango/GObject الأصلية. تثبتها صورة Docker تلقائيًا.
محليًا ثبّت GTK/Pango المتوافق وأضف مجلد المكتبات إلى `PATH`، أو نفّذ اختبارات
وتوليد PDF داخل Docker. الاختبار المتجاوز بسبب غياب هذه المكتبات لا يثبت مسار
PDF؛ CI Linux هو المرجع الكامل له.

## النشر

GitHub Actions يفحص lint، والأسرار، والاعتماديات، والهجرات، واختبارات الإنتاج
والصورة قبل نشر `main`. النشر الفعلي موثق في:

- `docs/CONTINUOUS_DEPLOYMENT_AR.md`
- `docs/PRODUCTION_LAUNCH_CHECKLIST.md`
- `compose.hetzner.yaml` و`deploy/hetzner/remote_deploy.sh`

نجاح CI أو نشر image لا يثبت وحده أن الإنتاج محدث؛ تحقق من SHA/وسم الإصدار
داخل الخدمة، والهجرات، وreadiness، والأصول العامة بعد النشر.

## بوابات الدين المعماري

- القياس المتكرر: `python scripts/architecture_metrics.py`.
- typecheck التدريجي: `python -m mypy`؛ نطاقه وأسباب حدوده في
  [TYPECHECK_BASELINE.md](TYPECHECK_BASELINE.md).
- جرد CSS وخطة خفض specificity في
  [docs/CSS_DEBT_AUDIT.md](docs/CSS_DEBT_AUDIT.md).
- تعامل الأسرار التاريخية وإجراء المسؤول البشري في
  [SECURITY_SECRET_ROTATION_PLAN.md](SECURITY_SECRET_ROTATION_PLAN.md).

لا تُشغّل `check --deploy` من shell يحمل `.env` محلية دون ضبط switches الخاصة
بالمزودين صراحةً؛ استخدم environment معزولة مثل CI حتى لا تختلط قيم التطوير
بعقد production.

## إضافة ميزة بأمان

1. حدّد المجال والعقد العام والصلاحيات وعزل المدرسة.
2. أضف characterization test قبل تعديل منطق قديم غير مغطى.
3. ضع منطق الأعمال في service، واحتفظ بالـview للتنسيق والتحقق والاستجابة.
4. أضف migration فقط عند حاجة البيانات، واختبر rollback/compatibility منطقيًا.
5. شغّل الفحوص والاختبارات أعلاه ثم حدّث الوثائق وعقد البيئة عند الحاجة.

## استكشاف الأخطاء

- خطأ import بعد التثبيت: تأكد أن البيئة الافتراضية مفعلة وأن Python 3.12 هو
  المستخدم الفعلي (`python --version`, `Get-Command python`).
- اختلافات migration: لا تنشئ migration عشوائيًا؛ راجع النموذج والفرع أولًا.
- Redis غير متاح محليًا: اترك عناوين Redis فارغة؛ لا تستخدم هذا الوضع لإثبات
  الأقفال أو rate limits الموزعة.
- PDF لا يعمل على Windows: استخدم Docker أو ثبّت مكتبات GTK/Pango/libmagic.
- فشل `collectstatic`: راجع مرجع asset المفقود داخل CSS/JS؛ تخزين الإنتاج
  المتشدد يكشف مراجع لا يكشفها runserver.
- مشكلة خاصة بالإنتاج: ابدأ بـ `/api/v1/health/` و`/api/v1/readiness/` والسجلات،
  ولا تعدّل بيانات الإنتاج أثناء التشخيص دون تفويض صريح.
