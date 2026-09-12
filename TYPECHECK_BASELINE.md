# TYPECHECK BASELINE

تاريخ التفعيل: 2026-09-12.

## Tool and gate

- Tool: `mypy 2.3.1` (development-only dependency).
- Local command: `python -m mypy`.
- CI gate: موجود ضمن `Static correctness checks` بعد Ruff.
- Strategy: قائمة ملفات صريحة في `pyproject.toml`، لا `strict` شامل ولا global
  error-code ignores.

## Baseline

| Metric | Before fixes | Enabled gate |
|---|---:|---:|
| Files checked | 7 | 7 |
| Errors | 23 | 0 |
| Warnings | 0 | 0 |
| Ignored error codes | 0 | 0 |
| `# type: ignore` suppressions in scope | 0 | 0 |
| Untyped public functions | 3 | 0 |

النطاق الحالي:

- `core/client_ip.py`
- `core/task_dispatch.py`
- `operations/task_names.py`
- `reports/services_capacity.py`
- `reports/services_generated_exports.py`
- `reports/services_notifications.py`
- `reports/task_names.py`

## Boundary rationale

`follow_imports = "skip"` يجعل النطاق تدريجيًا: imported Django/application
modules خارج القائمة لا تُفحص كجزء من هذه البوابة، لكن الملفات الستة نفسها
تخضع لـ`disallow_untyped_defs` و`check_untyped_defs`. هذا ليس suppression
للأخطاء داخل النطاق، ولا يخفيها بـglobal ignore.

## Errors closed

- أضيف نوع `HttpRequest` إلى public client-IP helpers.
- صار row-lock selector يعيد `GeneratedExportJob | None` صراحةً.
- صارت metadata الخاصة بالأرشيف typed ومتحققًا من وجودها قبل indexing.
- أزيل `getattr` غامض النوع على اسم ملف الأرشيف.
- ضُبط adapter الإشعارات الديناميكي: الـcallable typed، وحُصر `Any` في model
  classes القادمة من Django app registry حتى لا تعود دورة import.

## Next scope

1. `reports/view_access.py` وpermission selectors.
2. payment/integration service modules بعد إخراج reconciliation من views.
3. payment/integration gateway contracts بعد إخراج reconciliation من views.

لا يوسّع CI النطاق قبل جعل الوحدة الجديدة نظيفة؛ ولا تُضاف suppressions إلا
لموضع واحد مع سبب قابل للحذف.
