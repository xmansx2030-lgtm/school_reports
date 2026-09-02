# تدقيق Cloudflare — الفجوات بين الحافة والمشروع

النطاق `tawtheeq-ksa.com` — Zone `534cfec46543893f2e2a2549a98762bf`، خطة Free.
مُستخرج من Cloudflare API بتاريخ 2026-08-04 ومقارن بالمشروع.

الترتيب حسب الأثر.

---

## 1. حرج — عنوان الأصل مكشوف، وCloudflare قابلة للتجاوز بالكامل

```
A      origin-school-reports.tawtheeq-ksa.com  178.104.163.3   proxied: false   ← غير محمي
CNAME  tawtheeq-ksa.com      → origin-school-reports…          proxied: true
CNAME  www.tawtheeq-ksa.com  → origin-school-reports…          proxied: true
CNAME  app.tawtheeq-ksa.com  → origin-school-reports…          proxied: true
```

السجل الوحيد غير المحمي هو الذي تشير إليه بقية السجلات. أي شخص يستعلم عنه
يحصل على عنوان الخادم الحقيقي — وقد تأكّد ذلك عملياً:

```
nslookup origin-school-reports.tawtheeq-ksa.com → 178.104.163.3
المنافذ 80 و443 و22 مفتوحة ومتاحة مباشرةً من الإنترنت
```

**الأثر:** كل ما في هذا الملف من قواعد WAF وحدّ معدل وحماية DDoS يُتجاوَز
بالاتصال المباشر بالعنوان. والمنفذ 22 (SSH) مكشوف للعالم.

**الإصلاح، بالترتيب:**

1. جدار ناري على الأصل يقبل 80/443 من [نطاقات Cloudflare](https://www.cloudflare.com/ips/) فقط،
   ويقصر 22 على عناوينك الإدارية.
2. فعّل **Authenticated Origin Pulls** (حالياً `enabled: false`) ليرفض الأصل
   أي اتصال TLS لا يحمل شهادة عميل من Cloudflare.
3. غيّر اسم سجل الأصل إلى قيمة غير قابلة للتخمين — الاسم الحالي وصفيّ ومباشر.

---

## 2. حرج — قاعدة التخزين المؤقت تُبطل CSP nonce على الصفحة الرئيسية

قاعدة "Cache public pages" في `http_request_cache_settings`:

```
expr: http.request.uri.path eq "/" or starts_with(path,"/about") or starts_with(path,"/services")
params: {"cache":true,"edge_ttl":{"default":7200,"mode":"override_origin"}}
```

بينما `platform_landing` في [auth.py:1284-1316](../reports/views/auth.py#L1284-L1316) يفعل
عكس ذلك تماماً، بوعي كامل بالمشكلة:

```python
@never_cache
@cache_control(no_cache=True, must_revalidate=True, no_store=True, max_age=0)
...
# Some CDN cache rules can ignore the standard never_cache
response["CDN-Cache-Control"] = "no-store"
response["Cloudflare-CDN-Cache-Control"] = "no-store"
```

وضع `override_origin` يتجاهل هذه الرؤوس الثلاثة كلها — بما فيها الدفاع الذي
كُتب خصيصاً لهذا الاحتمال.

**الأثر الأمني:** صفحة `/` تحمل **CSP nonce لكل طلب**. تخزينها على الحافة
ساعتين يعني توزيع nonce واحد على كل الزوار طوال تلك المدة، فيصير قيمة عامة
معروفة. وnonce متوقّع يُلغي الغرض من CSP القائم على nonce بالكامل.

**الأثر الوظيفي:** تغييرات الأسعار لن تظهر لمدة ساعتين، خلافاً لما يوثّقه
[auth.py:1250](../reports/views/auth.py#L1250).

**الإصلاح:** احذف `/` من القاعدة. المسارات `/about` و`/services` غير موجودة
أصلاً في المشروع.

---

## 3. حرج — قاعدة WAF تحجب webhook الدفع

في `http_request_firewall_custom`، القاعدة "Block suspicious bots":

```
action: managed_challenge
expr:   lower(http.user_agent) contains "python" or "curl" or "wget" or "headless" …
```

بالإضافة إلى `bot_management.fight_mode: true` و`browser_check: on`.

مؤكَّد عملياً:

```
POST /payments/moyasar/callback/<batch_ref>/  (python-requests) -> 403 Cf-Mitigated
POST /payments/tamara/webhook/          (curl)            -> 403 Cf-Mitigated
POST /payments/tamara/webhook/          (Chrome UA)       -> 401 من Django (توكن اختباري مرفوض)
GET  /api/v1/                   (curl)            -> 403
GET  /healthz/                  (curl)            -> 200
GET  /                          (Chrome UA)       -> 200
```

وينطبق الخطر نفسه على `POST /payments/tamara/webhook/`: تمارا ترسل الإشعار
من خادم إلى خادم، وأي تحدٍ يتطلب JavaScript يمنع تأكيد التحصيل وتفعيل الاشتراك.

المعالج يعيد التحقق من الفاتورة لدى البوابة بنفسه، فالحماية قائمة في التطبيق —
لكن الحافة تمنع الطلب من الوصول أصلاً، فتضيع إشعارات البوابة بصمت.

**الإصلاح:** قاعدة `Skip` أعلى الترتيب في WAF Custom Rules:

```
(http.request.uri.path eq "/healthz/")
or starts_with(http.request.uri.path, "/payments/moyasar/callback/")
or (http.request.uri.path eq "/payments/tamara/webhook/")
or starts_with(http.request.uri.path, "/api/v1/")
```

Action `Skip` مع تعليم جميع المنتجات. ثم عطّل **Bot Fight Mode** — فهو لا
يقبل استثناءات ويطبّق التحدّي على كامل النطاق بغض النظر عن قواعد Skip.

---

## 4. عالٍ — تحديد المعدل يعمل على عنوان Cloudflare لا الزائر

`Caddyfile.fragment` كان يمرر `header_up X-Real-IP {remote_host}`، و`{remote_host}`
خلف Cloudflare هو **عنوان حافة Cloudflare** لا الزائر. و`client_ip_for_ratelimit`
في [core/client_ip.py](../core/client_ip.py) يثق بـ`X-Real-IP` لأن `REMOTE_ADDR`
هو عنوان حاوية Caddy الداخلي (ضمن `TRUSTED_PROXY_CIDRS`).

النتيجة: كل زوار مركز بيانات Cloudflare واحد يتشاركون دلو تحديد معدل واحداً —
مستخدمون شرعيون يحجبون بعضهم، ومهاجم واحد لا يُعزل.

**أُصلح في المستودع:** يمرَّر الآن `Cf-Connecting-Ip`. لكن الإصلاح **لا يكتمل
بدون البند 1** — ما دام الأصل مكشوفاً، يمكن لأي عميل مباشر انتحال الرأس.

---

## 5. عالٍ — DNSSEC معطّل

`dnssec.status: disabled`. لنطاق يعالج بيانات طلاب ومدفوعات، هذا نقص أساسي
يجعل انتحال DNS ممكناً. تفعيله في Cloudflare بنقرة، ثم إضافة سجل DS لدى المسجّل.

---

## 6. متوسط — HSTS: ثلاثة إعدادات متعارضة، وأضعفها يفوز

| المصدر | القيمة |
|---|---|
| Cloudflare (يفوز) | `max-age=15552000`، بلا subdomains، بلا preload |
| Caddy | `max-age=31536000; includeSubDomains` |
| Django ([settings.py:1052](../config/settings.py#L1052)) | `31536000` + subdomains + preload |

الحافة تستبدل رأس الأصل، فيضيع قصد الطبقتين الأخريين، ويستحيل إدراج النطاق
في قائمة preload.

**الإصلاح:** SSL/TLS → Edge Certificates → HSTS: سنة كاملة مع `Include subdomains`
و`Preload`. أو عطّل HSTS في Cloudflare ودع Caddy وDjango يتوليانه.

---

## 7. متوسط — قاعدة التخزين تُجمّد `sw.js` سبعة أيام

```
expr: path contains "/static/" or "/favicon.ico" or "/sw.js"
params: browser_ttl 604800 (7 أيام)، edge_ttl 2678400 (31 يوماً)، mode: override_origin
```

بينما [config/urls.py:13](../config/urls.py#L13) يخدم عامل الخدمة بـ
`@cache_control(no_cache=True, must_revalidate=True, max_age=0)` عمداً.

**الأثر:** تحديثات PWA لا تصل المستخدمين حتى أسبوع. أخرج `/sw.js` من القاعدة —
الملفات المبصومة تحت `/static/` وحدها هي المرشّح الصحيح للتخزين الطويل.

---

## 8. متوسط — إعادة كتابة JavaScript تصطدم بـ CSP الصارم

```
rocket_loader: on          ← يؤجل ويعيد ترتيب كل سكربتات الصفحة
email_obfuscation: on      ← يحقن سكربتاً في HTML
server_side_exclude: on
replace_insecure_js: on
```

المشروع يطبّق CSP بـ nonce لكل طلب ([reports/middleware.py:1152](../reports/middleware.py#L1152))،
ويوجد اختبار يتحقق أن كل سكربت مضمّن يحمل nonce
([core/tests.py:151](../core/tests.py#L151)). Rocket Loader تحديداً معروف بكسر
هذا النمط لأنه يحقن سكربتاً بلا nonce.

**الإصلاح:** عطّل Rocket Loader وEmail Obfuscation. المكسب في الأداء لا يوازن
كسر CSP، وBrotli وHTTP/3 مفعّلان أصلاً.

---

## 9. متوسط — قاعدة إعادة توجيه معطّلة تسبب حلقة لا نهائية إن فُعّلت

```
Redirect Rule "Root to app redirect"  (enabled: false)
  tawtheeq-ksa.com → https://app.tawtheeq-ksa.com{path}
```

بينما [Caddyfile.fragment:27](../deploy/hetzner/Caddyfile.fragment#L27) يعيد
`app.` → الجذر. تفعيل هذه القاعدة يُنتج حلقة إعادة توجيه لا نهائية.

**الإصلاح:** احذفها بدل تركها معطّلة.

---

## 10. منخفض — نقاط تستحق المراجعة

- **`0rtt: on`** — بيانات TLS 1.3 المبكرة قابلة لإعادة الإرسال. Cloudflare تقصرها
  على الطلبات الآمنة، لكن تعطيلها أنظف لتطبيق يعتمد على POST.
- **قاعدة حجب `/config`** — النمط `path contains "/config"` واسع؛ لا يصطدم بمسار
  حالي، لكنه قد يحجب مساراً مستقبلياً بلا سبب واضح.
- **`security_level: medium` + `challenge_ttl: 1800`** — معقول، لكن راجعه بعد
  إصلاح البند 3.

---

## 11. عالٍ — ‏`Permissions-Policy` من الحافة تُعطّل الإملاء الصوتي

*مُضاف بتاريخ 2026-09-02، بعد البنود أعلاه، فترتيبه هنا زمنيّ لا حسب الأثر.*

الرأس الحيّ لا يطابق [Caddyfile.fragment](../deploy/hetzner/Caddyfile.fragment):
يحوي `fullscreen=(self)` و`publickey-credentials-get=()`، ولا وجود لهما في
المشروع قط — ولا في تاريخه. فالحافة تكتب رأسها فوق رأس الأصل، كما في البند ٦.

```
الحيّ (Cloudflare): accelerometer=(), autoplay=(), camera=(), …, microphone=(), …
الأصل  (Caddy):     accelerometer=(), ambient-light-sensor=(), …, microphone=(self), …
```

**الأثر:** `microphone=()` ليست «اسأل المستخدم» بل «لا أحد، أبداً». يرفض
المتصفّح `getUserMedia` بـ `NotAllowedError` قبل أن تظهر نافذة الإذن، فبطاقة
«اكتب تقريرك بصوتك» معطّلة كلياً، وتطلب من المعلّم تفعيل إذن لا مكان لتفعيله:
الرأس يعلو أي منح يمنحه المستخدم. الأصل صحّح رأسه، ولا أثر لذلك ما دامت الحافة
تستبدله.

**الإصلاح:** Rules → Transform Rules (أو Managed Transforms إن كان الرأس من
قاعدة مُدارة) → عدّل `microphone=()` إلى `microphone=(self)`. أو أسقط
`Permissions-Policy` من الحافة ودع Caddy يتولّاه — قائمته أشمل أصلاً.

يحرس هذا [post_deploy_smoke.py](../scripts/post_deploy_smoke.py) الآن، فيفشل
النشر ما دامت الحافة تعيد المنع.

---

## ما تأكّد سليماً

- **البريد مُعدّ إعداداً صحيحاً.** `v=spf1 -all` على الجذر مع نطاق فرعي مخصّص
  للإرسال (`send.` بـ `include:amazonses.com`) وDKIM على `resend._domainkey`
  هو النمط الموصى به من Resend. وDMARC `p=reject; adkim=s; aspf=s` يمرّ عبر
  محاذاة DKIM الصارمة.
- `ssl: strict` — أقوى وضع اتصال بالأصل.
- شهادتان فعّالتان (Google + Let's Encrypt احتياطية) تغطيان الجذر والنطاقات الفرعية.
- `min_tls_version: 1.2`، `tls_1_3: zrt`، `http3: on`، `brotli: on`، `pq_keyex: on`.
- قاعدة "Bypass admin and auth" تغطي `/admin*` و`/login` و`/api` و`/dashboard`
  وتمنع تخزين الصفحات المصادَق عليها.
- قاعدة حجب مسارات هجمات WordPress وphpMyAdmin و`.git` و`.env`.
- `www` → الجذر بـ 301 عبر قاعدة إعادة توجيه سليمة.

---

## نقطة تحتاج قراراً

**`support@tawtheeq-ksa.com` لا يستقبل بريداً.** لا يوجد MX على الجذر
(سجل `send.` مخصّص لارتدادات SES)، وEmail Routing حالته `unconfigured`.
لكن `/.well-known/security.txt` ينشر هذا العنوان للتواصل الأمني
([settings.py:171](../config/settings.py#L171)).

إما تفعيل Cloudflare Email Routing لتحويله إلى صندوق قائم، أو تغيير
`SECURITY_CONTACT_EMAIL` إلى عنوان يعمل فعلاً.
