# CSS DEBT AUDIT

تاريخ القياس: 2026-09-12. خط الأساس هو المحتوى الملتزم في
`d471e24be28d71ad529acf87c307ec7ff07f315f`، وليس Working Tree المختلطة بعمل
واجهة متزامن.

## Counts

| Metric | HEAD baseline | Current worktree |
|---|---:|---:|
| `!important` total | 1,090 | 1,084 |
| `!important` in CSS | 640 | — |
| `!important` in templates | 450 | — |
| Files containing `!important` | 95 | — |
| Inline `style=` attributes | 75 | 75 |
| Templates containing inline styles | 17 | 17 |

فرق الستة بين HEAD وWorking Tree ناتج عن ملفات CSS خارج ملكية هذه المرحلة؛ لا
يُحتسب إزالةً من عمل architecture hardening حتى يدخل commit مستقل من مالكه.

## Highest concentrations at HEAD

| File | `!important` | Classification |
|---|---:|---|
| `static/css/dark-mode.css` | 168 | Temporary / needs component refactor |
| `static/css/app.css` | 159 | Legacy / mixed global responsibilities |
| `static/css/royal-theme.css` | 139 | Legacy theme overrides |
| `static/css/mobile-professional.css` | 63 | Responsive overrides; mixed required/legacy |
| `reports/templates/reports/platform_payments.html` | 49 | Component styles embedded in template |
| `static/css/app-components.css` | 34 | Component overrides; review per component |
| `reports/templates/reports/achievement_file.html` | 33 | Page-specific legacy overrides |
| `static/css/circulars-official.css` | 29 | Feature stylesheet; mixed required/legacy |
| `reports/templates/reports/achievement_my_files.html` | 27 | Page-specific legacy overrides |
| `reports/templates/reports/report_share_manage.html` | 25 | Page-specific overrides |

Dark-mode alone accounts for 168 uses and explicitly named responsive CSS for
63. لا توجد ملفات باسم RTL تحتوي `!important`؛ RTL overrides موزعة داخل
global/page CSS، ولذلك لا يصح اعتبار رقم filename-based صفر دليلًا على غيابها.

## Inline styles

أكبر مصدرين هما email templates:

- `emails/branded_base.html`: 35 attributes.
- `emails/password_reset_email.html`: 13 attributes.

هذه غالبًا **Required** لتوافق عملاء البريد ولا ينبغي نقلها تلقائيًا إلى
stylesheet خارجي. بقية 27 attribute تحتاج مراجعة صفحة بصفحة؛ ليست كلها دينًا
متساويًا.

## Classification model

- **Required:** email-client inline CSS، حالات accessibility أو vendor behavior
  المثبتة باختبار، وطبقات print/PDF التي لا تقبل cascade الاعتيادي.
- **Temporary:** override له تعليق/issue وحد زمني ومسار component واضح.
- **Legacy:** قاعدة جاءت من theme/page أقدم وتتصادم مع design system الحالي.
- **Removable:** declaration يساوي القيمة التي ستنتجها cascade بدونه، وتؤكد
  browser regression عدم الفرق.
- **Needs component refactor:** selector يعالج نفس المكوّن عبر global، theme،
  dark وmobile layers؛ الإصلاح الصحيح توحيد API المكوّن لا حذف العلم منفردًا.

## Root causes

1. تحميل `app.css` ثم theme/dark/mobile styles يجعل الطبقات اللاحقة تتنافس على
   المكوّن نفسه بدل استعمال tokens وcomponent states.
2. page-local `<style>` blocks تضيف specificity جديدة فوق design system.
3. dark mode وresponsive behavior يغيّران properties مباشرة بدل tokens في
   مواضع كثيرة.
4. قوالب features القديمة تكرر tables/cards/alerts مع selectors مختلفة.

## Token status and target

`static/css/design-system.css` هو موضع الملكية المقترح لـcolors, spacing,
radius, typography, shadows, transitions وz-index. قبل توسيعه يجب جرد variables
الحالية ومنع تعريف token ثانٍ للمعنى نفسه. لا تُغير هذه المرحلة الهوية البصرية.

الترتيب المستهدف للـcascade:

```text
tokens -> reset/base -> layout -> components -> feature variants -> utilities
                                             -> dark/RTL state via tokens
```

## Safe removal strategy

1. لا تُعدل الملفات الخمسة dirty (`app-shell.css`, `app.css`,
   `design-system.css`, `royal-theme.css`, `standalone-system.css`) ضمن commits
   هذه المرحلة.
2. اختر مكوّنًا واحدًا غير متداخل وله رحلة browser قابلة للتكرار.
3. التقط desktop `1440x900` وmobile `390x844` في RTL، وفي dark/light إن كانا
   متاحين.
4. أزل فقط overrides التي ثبت أنها redundant، ثم افحص overflow, modals,
   dropdowns, toasts, header, sidebar, tables وforms المتأثرة.
5. سجّل `Before / Removed / Remaining` لكل دفعة. لا يوجد هدف مصطنع للوصول إلى
   صفر.

## Current decision

هذه الدفعة audit-only لأن أعلى ملفات الدين متداخلة مع عمل UI خارجي غير ملتزم.
التعديل الآن سيكسر attribution وقابلية التراجع. تخفيض آمن يبدأ بعد عزل ذلك
العمل أو اختيار feature stylesheet غير متداخل مع browser contract مخصص.
