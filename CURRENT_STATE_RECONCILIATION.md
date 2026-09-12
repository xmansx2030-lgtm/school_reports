# CURRENT STATE RECONCILIATION

تاريخ الالتقاط: 2026-09-12 (Asia/Riyadh). هذا الملف هو نقطة دخول مرحلة
Architecture & Technical Debt Hardening، ويصف الحالة بعد `git fetch --prune
origin` وقبل أي تعديل برمجي جديد في المرحلة.

## Repository state

| البند | القيمة |
|---|---|
| Current HEAD | `d471e24be28d71ad529acf87c307ec7ff07f315f` |
| Branch | `main` |
| origin/main | `27e09c061edcc0a87013d3fb4a2de1c2d9a31980` |
| Merge base | `27e09c061edcc0a87013d3fb4a2de1c2d9a31980` |
| Ahead / behind | `+5 / -0` |
| Staged files | 0 |
| Dirty tracked files | 18 |
| Untracked files | 0 |
| Dirty diff | 413 additions / 176 deletions |

نقطة الأساس التاريخية للتقرير السابق هي
`9a45c5107fcc120dbf7ab99ac952187da889d087`، لكن نقطة بدء هذه المرحلة الفعلية
هي Current HEAD أعلاه؛ لا يجوز الرجوع إلى baseline القديم أو إسقاط عمل أحدث.

## Local commits not in origin/main

1. `12ec982b` — `docs: publish refactoring handover report`
2. `74ad0e1b` — `refactor: decouple file cleanup retry dispatch`
3. `780d89d2` — `refactor: extract generated export service`
4. `ebed184b` — `docs: record architecture hardening phase`
5. `d471e24b` — `docs: consolidate refactor conversation summary`

منذ التقرير الموحد الحالي في `d471e24b` لا توجد commits لاحقة قبل بدء هذه
المرحلة. تحرك `origin/main` السابق موثق حتى `27e09c06` فقط؛ لا توجد هنا قرينة
على نشر commits المحلية الخمسة أو وصولها إلى production.

## Concurrent uncommitted work

الملفات التالية معدلة خارج commits الخاصة بالـrefactor. طبيعتها المتماسكة
(Templates وCSS واختبارات تجربة الواجهة)، وتوقيتاتها بين 14:27 و14:49 في
2026-09-12، وسبق ظهورها أثناء المرحلة السابقة، تجعل attribution الآمن لها:
**UI work متزامن/خارجي محفوظ، وليس جزءًا من هذه المرحلة**.

- `reports/templates/base.html`
- `reports/templates/reports/edit_report.html`
- `reports/templates/reports/edit_teacher.html`
- `reports/templates/reports/login.html`
- `reports/templates/reports/maintenance_mode.html`
- `reports/templates/reports/my_subscription.html`
- `reports/templates/reports/partials/_meeting_copy_link.html`
- `reports/templates/reports/password_reset_base.html`
- `reports/templates/reports/register_school.html`
- `reports/templates/reports/registration_success.html`
- `reports/templates/reports/user_guide.html`
- `reports/tests/test_mobile_experience.py`
- `reports/tests/test_notification_alert_experience.py`
- `static/css/app-shell.css`
- `static/css/app.css`
- `static/css/design-system.css`
- `static/css/royal-theme.css`
- `static/css/standalone-system.css`

لن تستخدم هذه المرحلة `git add -A`، ولن تحذف أو تستبدل هذه التغييرات. أي عمل
CSS/Template سيبدأ بجرد read-only، ويتجنب الملفات المتداخلة أو يؤجل تعديلها
إلى أن توجد ملكية واضحة ومسار دمج مستقل.

## Attribution boundaries

- commits حتى `27e09c06`: موجودة في `origin/main` وتمثل تنفيذ المرحلة الأساسية.
- commits من `12ec982b` إلى `d471e24b`: عمل refactor/handover محلي سابق موثق.
- الملفات الـ18 أعلاه: عمل واجهة غير ملتزم لا ينسب إلى هذا الـrefactor.
- ملفات وتقارير المرحلة الجديدة: تُضاف في commits صغيرة بملفات staged صريحة.

## Risks before implementation

1. خمسة commits محلية لم تصل إلى `origin/main`؛ أي قياس يجب أن يعتمد `HEAD` لا
   remote، وأي push/deploy خارج التفويض الحالي.
2. شجرة العمل dirty؛ فشل اختبار قد يأتي من التغييرات المتزامنة، لذلك يلزم
   baseline واختبارات مجال بعد كل شريحة مع فحص diff صريح.
3. ملفات CSS والقوالب الأعلى دينًا تتداخل مع العمل الخارجي؛ القياس ممكن الآن،
   لكن refactor بصري عليها مخاطرة دمج حتى تُعزل الملكية.
4. PostgreSQL/Redis/Celery/R2/payment/Firebase تحتاج خدمات أو credentials معزولة؛
   لا يجوز تحويل غياب البيئة إلى ادعاء نجاح أو استعمال production.
5. تاريخ Git قد يحتوي credentials قديمة؛ يمنع history rewrite أو force push
   دون تصريح ودليل تدوير مستقل.
6. import hubs وواجهات التوافق قد تُستهلك ديناميكيًا؛ لا يُحذف wildcard أو
   public export قبل جرد المستهلكين واختبار عقد.

## Safe starting decision

ابدأ بقياسات dependency/complexity/exception/CSS/type الحالية، ثم أصلح شريحة
Backend لا تتداخل مع الملفات الـ18، وتسبق كل عملية نقل اختبارات characterization.
تبقى الاختبارات والبناء محلية/Sandbox، ولا تنفذ عمليات production.
