# ADR 003: Constrain compatibility import surfaces before removal

- Status: Accepted
- Date: 2026-09-12

## Context

`reports.models`, `reports.views` وملفات billing هي أسطح عامة تاريخية تعتمد
عليها URLs، migrations، admin واختبارات كثيرة. حذف wildcard imports دفعة واحدة
قد يكسر أسماء تُحل ديناميكيًا ولا يكشفها البحث النصي.

## Decision

- لا يضاف implementation جديد إلى compatibility hubs.
- الوحدات الجديدة تستورد مالك الاسم مباشرةً.
- كل دفعة تحول المستهلكين إلى imports صريحة، ثم تقيد `__all__` وتضيف contract
  للأسماء التي تبقى عامة.
- migrations المنشورة تظل قادرة على استيراد helpers التاريخية.

## Consequences

بقيت بعض wildcards والدورات معلنة P1؛ قبولها مؤقت وليس تصريحًا بتوسيعها. إزالة
السطح بالكامل تحتاج inventory للمستهلكين واختبارات URL/model migration، ولذلك
لم تُنفذ ضمن refactor غير مرتبط.

