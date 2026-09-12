# ADR 001: Neutral task dispatch boundary

- Status: Accepted
- Date: 2026-09-12

## Context

كانت services وfile cleanup وforms تستورد `reports.tasks`، بينما tasks تستورد
الخدمات نفسها. نتجت cycles، وصار import Celery شرطًا لاستعمال منطق المجال.

## Decision

- أسماء المهام العامة ثوابت في `reports/task_names.py` و`operations/task_names.py`.
- المنتجون ينشرون عبر `core/task_dispatch.py` بالاسم، بلا import للتنفيذ.
- business logic ينتقل إلى `reports/services_*.py`، وتبقى task غلافًا رقيقًا.
- eager-mode يحمل task registry عند الحاجة، وتبقى fallback policy مركزية.

## Consequences

تحافظ أسماء المهام والـqueues والمراقبة على التوافق، وتنخفض الدورات. يحتاج كل
task منقول contract test للاسم، eager/fallback، وidempotency. لا يجوز استعمال
local import لإخفاء دورة جديدة بلا سبب موثق.

