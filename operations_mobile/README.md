# مركز العمليات - Flutter

تطبيق Android مخصص لمشرف النظام لمتابعة `school-reports-prod` والمشاريع المستضافة عليه.

تفاصيل إدارة Hetzner وقراءة السجلات وتجهيز مسار الطوارئ في [دليل إدارة الخادم](../docs/OPERATIONS_SERVER_MANAGEMENT_AR.md).

## التشغيل المحلي

```powershell
flutter run --dart-define=OPS_API_BASE_URL=http://10.0.2.2:8000/api/operations/v1
```

يستخدم المحاكي `10.0.2.2` للوصول إلى خادم Django على جهاز التطوير. في نسخة الإنتاج، القيمة الافتراضية هي:

`https://tawtheeq-ksa.com/api/operations/v1`

## تفعيل تنبيهات Firebase

أنشئ تطبيق Android في Firebase بالمعرف `com.tawtheeq.tawtheeq_operations`، ثم شغّل التطبيق بهذه القيم العامة:

```powershell
flutter run `
  --dart-define=FIREBASE_API_KEY=... `
  --dart-define=FIREBASE_APP_ID=... `
  --dart-define=FIREBASE_MESSAGING_SENDER_ID=... `
  --dart-define=FIREBASE_PROJECT_ID=...
```

على الخادم فقط، ضع ملف حساب خدمة Firebase خارج المستودع واضبط:

```text
FCM_PROJECT_ID=your-project-id
GOOGLE_APPLICATION_CREDENTIALS=/run/secrets/firebase-service-account.json
```

لا تضف ملف حساب الخدمة أو مفتاح توقيع Android إلى Git.
