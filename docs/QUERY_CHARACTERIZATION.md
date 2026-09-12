# QUERY CHARACTERIZATION

أعيد تشغيل هذه العقود في 2026-09-12 على PostgreSQL 16 معزول. الهدف قياس النمو
مع حجم البيانات، لا فرض رقم هش على كل تعديل مشروع في middleware أو navigation.

## Recorded contracts

| Surface | Dataset | Smaller run | Larger run | Contract |
|---|---|---:|---:|---|
| Platform schools directory | 1 base + 3 ثم +9 schools | 10 queries | 9 queries | larger ≤ smaller |
| Executive group dashboard | 3 ثم +6 group schools | 17 queries | 16 queries | larger ≤ smaller |
| Teacher reports selector | 8 reports, materialize first 5 | 2 queries | 2 queries | exact joined + evidence prefetch |
| Navigation context — teacher | one active-school role | 23 queries | 23 ceiling | exact budget |
| Navigation context — manager | one active-school role | 19 queries | 19 ceiling | exact budget |
| Navigation context — officer | one active-school role | 24 queries | 24 ceiling | exact budget |
| Dashboard storage pressure | one school | ≤5 queries | ≤5 ceiling | cheaper than full overview |

انخفاض query count في العينة الأكبر سببه اختلاف مسارات cache/initialization
الصغيرة، وليس optimization جديدًا. كان الاختبار القديم يستخدم equality، ولذلك
فشل على `10→9` و`17→16` رغم عدم وجود N+1؛ صحح العقد إلى منع **الزيادة** فقط.

## Before / after

لم تتغير ORM selectors في هذه الدفعة، ولذلك لا يُنسب خفض أداء مصطنع:

| Metric | Before | After |
|---|---:|---:|
| Query-growth regressions | 0 known | 0 |
| Cross-database fragile equality assertions | 2 | 0 |
| PostgreSQL query-characterization failures | 2 | 0 after correction |

لم يُستخدم timing كحكم pass/fail لأن ضوضاء Windows/Docker أكبر من الفروق في
هذه البيانات الصغيرة. الأرقام الزمنية تحتاج benchmark مستقل بعدة تكرارات
وpercentiles؛ بوابة CI الحالية تحرس query shape/count فقط.

## Test locations

- `reports/tests/test_consumption_panel.py`
- `reports/tests/test_executive_director.py`
- `reports/tests/test_reports_performance.py`
- `reports/tests/test_nav_context_contract.py`
- `reports/tests/test_storage_controls_and_alerts.py`
- `reports/tests/test_scaling_protections.py`

أي optimization لاحق يجب أن يضيف dataset ثابتًا ويسجل count قبل/بعد، ثم يبقي
العقد الذي يمنع النمو. caching ليس بديلًا افتراضيًا عن `select_related`,
`prefetch_related`, annotations أو`Exists`.
