# ADR 002: Progressive mypy boundary

- Status: Accepted
- Date: 2026-09-12

## Context

تفعيل strict typing على أكثر من مئة ألف سطر دفعة واحدة ينتج suppressions عامة
ويمنع التبني العملي.

## Decision

تعمل بوابة `python -m mypy` على قائمة صغيرة صريحة عالية القيمة في
`pyproject.toml`. كل توسعة تجعل الوحدة الجديدة صفر أخطاء قبل إضافتها. لا توجد
global ignores أو baseline يخفي diagnostics، و`follow_imports=skip` يحصر
المسؤولية في الملفات المعلنة.

## Consequences

النطاق الحالي حقيقي وقابل للزيادة، لكنه لا يعني أن بقية المشروع typed. توسع
المرحلة التالية permissions/authentication ثم integrations/payment services.

