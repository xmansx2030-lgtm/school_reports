# دليل الإطلاق الرسمي — منصة توثيق

**آخر تحديث:** 2026-08-11
**المصدر:** [SECURITY_AUDIT_REPORT.md](SECURITY_AUDIT_REPORT.md) · [CRITICAL_FIXES.md](CRITICAL_FIXES.md) · [SECURITY_REMEDIATION_PLAN.md](SECURITY_REMEDIATION_PLAN.md)

هذه الوثيقة هي **ما تنفّذه أنت**. كل ما يمكن تنفيذه داخل المستودع نُفِّذ بالفعل؛ ما بقي يحتاج وصولاً للخادم أو لوحات الخدمات، أو قرارًا تشغيليًا.

---

## القاعدة الحاكمة

**لا تعتمد على هذه الوثيقة وحدها.** كل بند أدناه يحرسه فحص آلي يفشل إن أُغفل:

| الفحص | متى يعمل | ما يمنعه |
|---|---|---|
| `manage.py production_preflight` | بعد كل نشر (خطوة CI) | سرّ مسرَّب، حدود بلا مخزن، روابط طويلة العمر، وسائط عامة |
| `scripts/post_deploy_smoke.py` | بعد كل نشر (خطوة CI) | ترويسات ناقصة، CDN في CSP، كاش مشترك، تسريب ملفات |
| `scripts/check_no_tracked_secrets.py` | pre-commit + CI | عودة `.env` إلى التتبع |
| `scripts/check_requirements_lock.py` | pre-commit + CI | قفل اعتماديات لا يواكب `requirements.txt` |
| `pip-audit -r requirements.lock.txt` | CI | ثغرة معروفة في أي حزمة تدخل الصورة |

فإن نسيت بندًا، **سيفشل النشر ويقول لك أيّهما**.

---

## ⚠️ الترتيب إلزامي

**انشر أولًا، ثم أضف متغيّرات البيئة.** والسبب واحد ومحدَّد: `LOGIN_THROTTLE_FAIL_CLOSED` مشتقّ تلقائيًا من وجود `REDIS_LIMITS_URL`. فإن أضفتَ العنوان **قبل** أن تُنشأ حاوية `redis-limits` — وهي تُنشأ بالنشر لأن تعريفها في `compose.hetzner.yaml` المعدَّل — صار الفشلُ مغلقًا على مخزنٍ غير موجود، أي **منعَ دخولٍ للجميع**.

الترتيب أدناه يتجنّب ذلك تمامًا. لا تقفز بين الخطوات.

**الخادم:** `178.104.163.3` (`origin-school-reports.tawtheeq-ksa.com`) · **المسار:** `/opt/school_reports`

---

## الخطوة 1 — النشر

```bash
git push origin main    # أو ادمج الـ PR
```

هذا ما يُنشئ حاوية `redis-limits` (كل خدمات التطبيق تعتمد عليها في `compose.hetzner.yaml`).

خطوات CI التي تحرس الإصدار — أي إخفاق فيها يُفشل النشر:

1. `Secrets hygiene` — لا ملف أسرار متتبَّع
2. `Security checks` — `pip-audit` على القفل + `bandit`
3. `Dependency lock is in sync`
4. `Production deployment checks` — `check --deploy --fail-level WARNING`
5. `Test` — 1549 اختبارًا
6. `Verify the running release` — `production_preflight` داخل الحاوية الحيّة
7. `Smoke-test the public site` — الترويسات الحقيقية من الخارج

> **متوقَّع بعد أول دفعة:** الخطوة 6 **ستفشل حمراء** برسالة `Secret still matches a value leaked in git history`. هذا الحارس يعمل كما صُمِّم: التطبيق يكون قد نُشر ويعمل، والفشل عند التحقق لا عند النشر. تختفي الحمرة بعد الخطوة 3.

---

## الخطوة 2 — متغيّرات البيئة (بعد نجاح النشر)

```bash
ssh <USER>@178.104.163.3
cd /opt/school_reports

# نسخة احتياطية قبل أي تعديل
cp deploy/hetzner/env.production \
   deploy/hetzner/env.production.bak.$(date +%Y%m%dT%H%M%S)

# تأكّد أن الحاوية الجديدة تعمل قبل توجيه العدّادات إليها
docker compose -f compose.hetzner.yaml ps redis-limits

REDIS_PW=$(grep '^REDIS_PASSWORD=' deploy/hetzner/env.redis | cut -d= -f2-)
cat >> deploy/hetzner/env.production <<EOF
REDIS_LIMITS_URL=redis://:${REDIS_PW}@redis-limits:6379/0
REDIS_LIMITS_MAXMEMORY=96mb
AWS_QUERYSTRING_EXPIRE=900
AUDIT_LOG_RETENTION_DAYS=365
EOF
```

> `REDIS_PASSWORD` من `deploy/hetzner/env.redis` — نفسه الذي تستعمله الخدمة الجديدة. رمّزه بـ URL-encoding إن حوى `@` أو `/` أو `:`، وإلا انكسرت السلسلة.
>
> بمجرد وجود `REDIS_LIMITS_URL`، يصير `LOGIN_THROTTLE_FAIL_CLOSED=True` تلقائيًا. لا تضبطه يدويًا.

---

## الخطوة 3 — ~~تدوير المفتاح المسرَّب~~ ✅ غير مطلوب

**تُحقِّق على الإنتاج بتاريخ 2026-08-11:** المفتاح الجاري (88 محرفًا، 50 محرفًا مميزًا) **ليس** أيًّا من المفاتيح الأربعة التي ظهرت في تاريخ Git. و`DATABASE_URL` كذلك، وهو يشير إلى `postgres` داخل شبكة Docker الداخلية لا إلى مثيل Render المسرَّب.

**لا تدوير مطلوبًا، ولا إسقاط للجلسات.**

الحارس الآلي باقٍ ويعمل: `production_preflight` سيفشل إن عاد أيٌّ من هذه القيم يومًا.

**يبقى عليك بندان صغيران:**
- [ ] احذف مفاتيح Cloudinary من لوحة تحكمها (لم تعد مستخدمة — المنصة على R2)
- [ ] تأكّد من تفكيك قاعدة بيانات Render القديمة إن كانت ما زالت قائمة

> **إن دوّرت سرًّا مكشوفًا مستقبلًا:** أضف تجزئة القيمة القديمة إلى [core/compromised_secrets.py](core/compromised_secrets.py) — لا تحذف الموجود. القائمة سجلٌّ تراكمي لما لا يجوز أن يعود.

---

## ⛔ قاعدة تشغيلية — لا تشغّل compose يدويًا بلا `APP_IMAGE`

`compose.hetzner.yaml` كان يسقط على الوسم `school-reports:local` عند غياب `APP_IMAGE`. أي `docker compose up -d` يدوي — بعد تعديل متغيّر بيئة مثلًا — كان **يستبدل صورة الإنتاج بصمت**، فيفشل `collectstatic` بخطأ صلاحيات مضلِّل ويتوقف الموقع.

**أُصلح على مستويين:**
1. `${APP_IMAGE:?...}` — compose يرفض التشغيل ويقول السبب بدل أن يخمّن.
2. `remote_deploy.sh` يثبّت الوسم المنشور في `/opt/school_reports/.env`، وcompose يقرأه تلقائيًا. (كُتب الملف على الخادم يدويًا بالفعل.)

فالأوامر اليدوية تعمل الآن على الصورة الصحيحة بلا أن يتذكر أحد تصديرها. وللتراجع إلى إصدار أقدم:

```bash
APP_IMAGE=ghcr.io/xmansx2030-lgtm/school_reports:<sha> \
  bash deploy/hetzner/remote_deploy.sh
```

---

## الخطوة 4 — الاختبار اليدوي (لا يمكن أتمتته)

تقصير عمر الروابط الموقَّعة من 24 ساعة إلى 15 دقيقة هو التغيير الوحيد الذي قد يظهر أثره في مسار مستخدم حقيقي. اختبر بعد النشر:

- [ ] تنزيل **ZIP بيانات المدرسة** (مدير مدرسة)
- [ ] فتح **إيصال دفع** (لوحة المنصة)
- [ ] فتح **مرفق تعميم** وشاهد إنجاز
- [ ] تنزيل **PDF ملف الإنجاز**
- [ ] فتح **لوحة مدير المدرسة** ولوحة المنصة — تأكّد أن **الرسوم البيانية تظهر** (Chart.js صار مُستضافًا محليًا بعد إزالة الـ CDN من CSP)

> المتوقَّع: الأربعة الأولى تعمل بلا تغيير — الروابط تُولَّد وقت العرض. الخامس هو ما يستحق نظرة فعلية.

---

## الخطوة 5 — تفعيل Sentry (موصى به بشدة)

سجلاتك اليوم تذهب إلى `console` فقط، وسائق سجلات Docker مضبوط على `max-size: 10m, max-file: 3` — أي **نافذة 30MB ثم تُمحى إلى الأبد**. وفي القاعدة 292 موضعًا يبتلع الاستثناء بصمت (`except Exception: pass`)، فتتوقّف الميزة ولا يعرف أحد.

المشروع مُهيَّأ بالكامل: المكتبة مثبَّتة، والتهيئة في [config/settings.py](config/settings.py)، و`send_default_pii=False`، ومُنقٍّ للبيانات الشخصية (`before_send=_sentry_scrub`) ينزع كلمات المرور والتوكنات وأرقام الجوال والهوية — **بما فيها متغيّرات الإطارات المحلية**، وهي أخطر مصدر.

```bash
# deploy/hetzner/env.production
SENTRY_DSN=https://<key>@o<org>.ingest.sentry.io/<project>
```

الخطة المجانية (5,000 خطأ/شهر) كافية لمنصة بحجمك.

---

## الخطوة 6 — بعد الإطلاق

**الأسبوع الأول:**
- [ ] راقب عدّاد `auth.login.throttle_store_unavailable` عبر `/ops/metrics/` — أي ظهور يعني أن `redis-limits` يتعثّر، وهو ما يمنع الدخول الآن
- [ ] راقب `redis-limits` عبر `docker stats` — يجب أن يبقى بعيدًا عن 96MB
- [ ] راقب حجم `reports_auditlog` بعد تمديد الاحتفاظ إلى سنة

**الشهر الأول:**
- [ ] **P2-1** — توكن مشترك لـ `moyasar_callback` ([SECURITY_REMEDIATION_PLAN.md](SECURITY_REMEDIATION_PLAN.md) المرحلة 4.1). مؤجَّل عمدًا: يغيّر المسار، فحدِّث لوحة Moyasar **قبل** النشر. مهمّة `reconcile-pending-gateway-payments` (كل 20 دقيقة) تعمل كشبكة أمان أثناء الانتقال.
- [ ] فعّل حرّاس ما قبل الالتزام للفريق: `pip install pre-commit && pre-commit install`
- [ ] احذف `db.sqlite3.backup-before-roles-migration` (17.8MB) بعد تأكيد الترحيل

**عند بلوغ العتبات** (موثَّقة في `config/settings.py`):
- 500 مدرسة → عامل منفصل لطابور `images`
- اقتراب `WEB_CONCURRENCY × MAX_CONCURRENT_REQUESTS` من `max_connections` → `docker compose --profile pgbouncer up -d`
- ذاكرة كاش Redis > 200MB → مثيل `REDIS_CACHE_URL` مستقل

---

## ما بقي مقبولاً بوعي

| البند | لماذا مقبول |
|---|---|
| `moyasar_callback` بلا توكن | لا يصدّق الحمولة أبدًا — يعيد الاشتقاق من البوابة. `batch_ref` 64 بت + حدّ 60/دقيقة |
| `style-src 'unsafe-inline'` | القوالب تستعمل `style="..."`؛ إزالته تحتاج نقل كل الأنماط إلى ملفات — تغيير واسع بلا مكسب أمني يوازيه |
| مفاتيح إصدار اللوحة بلا TTL | ~400KB عند 5,000 مدرسة مقابل سقف 384MB — مهمَل |
| تاريخ Git ما زال يحوي `.env` | التدوير (الخطوة 1) يبطل القيمة؛ إعادة كتابة التاريخ تحتاج تنسيقًا مع كل من يملك نسخة |

---

## حالة الجاهزية

| | |
|---|---|
| ثغرات Critical | **0** |
| ثغرات High | **0 متبقية في الكود** — SEC-002 أُصلح، SEC-001 محروس آليًا بانتظار التدوير |
| الاختبارات | **1549 — OK** |
| `ruff` / `bandit` / `pip-audit` / `check --deploy` | **نظيفة** |

**بعد الخطوتين 1 و2 والنشر:**

# 🟢 READY FOR PRODUCTION
