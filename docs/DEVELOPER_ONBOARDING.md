# Developer onboarding

## أول ساعة

1. اقرأ `README.md` ثم `docs/ARCHITECTURE.md` و`TECHNICAL_DEBT.md`.
2. أنشئ `.venv` من Python 3.12 وثبّت `requirements-dev.txt`.
3. انسخ `.env.example` إلى `.env` بقيم تطوير فقط.
4. نفّذ migrations و`python manage.py check` ثم شغّل الخادم.
5. قبل التعديل نفّذ `git status --short`؛ لا تفترض أن الملفات dirty تخصك.

## الخدمات المحلية

- SQLite + locmem تكفيان للتطوير البسيط، لا لإثبات سلوك الإنتاج.
- PostgreSQL: اضبط `DATABASE_URL` إلى قاعدة test/sandbox، ثم `migrate`.
- Redis: broker/Channels في DB 0 والكاش/الجلسات/locks في DB 1 وفق البيئة.
- Celery workers مقسمة إلى `default`, `notifications`, `images`, `periodic`؛
  راجع routing في `config/settings.py` قبل إضافة task.
- R2 والدفع وFCM والبريد لا تُختبر بمفاتيح إنتاج محليًا. استخدم sandbox أو
  العقود المmocked الموجودة.

مثال PowerShell لخدمات محلية معزولة (غيّر كلمات المرور المحلية ولا تنسخها إلى
Git؛ اختر أسماء/منافذ لا تصطدم بحاويات أخرى):

```powershell
docker run --rm --name school-reports-postgres -e POSTGRES_DB=school_reports_dev -e POSTGRES_USER=school_reports_dev -e POSTGRES_PASSWORD=local-only-password -p 55432:5432 postgres:16
docker run --rm --name school-reports-redis -p 56379:6379 redis:7.4-alpine
```

بعد ضبط `DATABASE_URL`, `REDIS_URL` و`REDIS_CACHE_URL` في shell آخر:

```powershell
python manage.py migrate
python -m celery -A config worker --loglevel=info --pool=solo -Q default,notifications,images,periodic
python -m celery -A config beat --loglevel=info
```

`--pool=solo` مناسب لفحص Windows المحلي فقط؛ حدود الإنتاج/concurrency موثقة
في `compose.hetzner.yaml`. لا تستخدم قاعدة أو Redis مشتركين مع production.

## أوامر البوابات

```powershell
$env:DJANGO_SETTINGS_MODULE = "config.test_settings"
python -m ruff check .
python -m mypy
python -m compileall -q config core reports maintenance operations scripts deploy manage.py
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test --verbosity 1 --noinput
python scripts/check_env_example.py
python scripts/check_requirements_lock.py
python scripts/check_no_tracked_secrets.py
python scripts/architecture_metrics.py
```

تشغيل PostgreSQL/Redis integration موثق في الاختبارات ذاتها؛ لا تفعل
`RUN_REDIS_INTEGRATION` إلا مع Redis معزول. PDF المرجعي وبيئة الإنتاج يُفحصان
داخل Docker لأن Windows قد يفتقد GObject/Pango.

## أين تضع ميزة جديدة

- HTTP/API validation والاستجابة: `reports/views/` أو `reports/forms.py`.
- تغيير حالة أو سير عمل: `reports/services_<domain>.py` مع transaction.
- قراءة مركبة reusable: selector/service المجال.
- تكامل خارجي: client/gateway واحد يملك timeout والتحقق والتسجيل.
- مهمة خلفية: thin wrapper في `reports/tasks.py` يستدعي service؛ المنتج ينشر
  باسم ثابت من `reports/task_names.py` عبر `core/task_dispatch.py`.
- model: الجزء المالك داخل `reports/model_parts/` مع توافق `reports.models`
  عند الحاجة.
- UI متكرر: include أو JS module؛ لا تضف inline script بلا CSP nonce.

## العقود التي لا تكسر

- URLs وأسماء Celery tasks وأسماء form fields وبنية JSON المستخدمة من PWA أو
  Flutter أو provider callbacks.
- عزل `request.active_school` و`SchoolMembership`.
- idempotency في payment callbacks/jobs و`effects_applied_at`.
- خصوصية الوسائط وروابط R2 الموقعة.
- التوافق في `reports.models`, `reports.views` وواجهات billing حتى تفكيكها
  عبر دفعة مستقلة واختبارات عقد.

## Flutter

من `operations_mobile/`:

```powershell
flutter pub get
flutter analyze
flutter test
flutter build apk --debug
```

مفتاح Android داخل `google-services.json` ليس service-account secret، لكن
قيوده على package/SHA/API تُراجع في Firebase/Google Cloud خارجيًا.

## قبل التسليم

- حدّث التقارير والـADR عند قرار معماري طويل الأثر.
- شغّل البوابات كاملة وسجل العدد والبيئة والنتيجة النهائية، لا بداية stream.
- راجع diff وstatus واستبعد ملفات debug والـartifacts والأسرار.
- لا تخلط commit refactor مع تغييرات واجهة أو عمل مطور آخر.
