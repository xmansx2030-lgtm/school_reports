# النشر التلقائي إلى Hetzner

كل دفعة (`push`) إلى `main` تمرّ بهذا المسار:

```
push → اختبارات CI (ruff, bandit, pip_audit, check --deploy, tests)
     → بناء صورة Docker ورفعها إلى ghcr.io بوسم الـ commit SHA
     → SSH إلى الخادم ونسخ ملفات compose
     → docker compose up -d
        └── خدمة migrate: migrate --noinput + collectstatic  ← الترحيلات تلقائية
        └── web/worker/beat تنتظر نجاح migrate قبل الإقلاع
     → انتظار healthcheck الخاص بـ web قبل اعتبار النشر ناجحاً
```

إذا فشل أي اختبار، لا يحدث نشر. وإذا فشل الترحيل، لا يقلع `web` أصلاً — فلا يُقدَّم تطبيق على قاعدة بيانات نصف مُرحَّلة.

الملفات: [.github/workflows/ci.yml](../.github/workflows/ci.yml) (وظيفة `deploy`)
و [deploy/hetzner/remote_deploy.sh](../deploy/hetzner/remote_deploy.sh) (ما يُنفَّذ على الخادم).

## الإعداد لمرة واحدة

### 1. مفتاح SSH للنشر

على جهازك:

```bash
ssh-keygen -t ed25519 -C "github-actions-deploy" -f ~/.ssh/school_reports_deploy -N ""
ssh-copy-id -i ~/.ssh/school_reports_deploy.pub <USER>@<SERVER_IP>
ssh-keyscan -H <SERVER_IP>            # المخرجات تذهب إلى DEPLOY_KNOWN_HOSTS
```

### 2. أسرار GitHub

وظيفة النشر تعمل داخل بيئة `production`، فموضع الأسرار الصحيح هو:

`Settings → Environments → production → Environment secrets`

وليس أسرار المستودع العامة. الفرق ليس شكلياً: أسرار البيئة تخضع لقواعد الحماية
(المراجعون، الفروع المسموحة)، وأسرار المستودع مكشوفة لأي وظيفة في أي فرع.

| الاسم | القيمة |
| --- | --- |
| `DEPLOY_SSH_HOST` | عنوان IP الخادم في Hetzner |
| `DEPLOY_SSH_USER` | مستخدم SSH (مثلاً `deploy` أو `root`) |
| `DEPLOY_SSH_KEY` | محتوى `~/.ssh/school_reports_deploy` كاملاً (المفتاح الخاص) |
| `DEPLOY_KNOWN_HOSTS` | مخرجات `ssh-keyscan -H <SERVER_IP>` |
| `DEPLOY_PATH` | اختياري — مسار المشروع على الخادم، الافتراضي `/opt/school_reports` |
| `DEPLOY_SSH_PORT` | اختياري — الافتراضي `22` |

يهيئ مسار النشر مخزن عدادات الحدود المنفصل تلقائيًا من `REDIS_PASSWORD`
المحفوظ داخل `env.production` على الخادم، ويكتب `REDIS_LIMITS_URL` مع
`LOGIN_THROTTLE_FAIL_CLOSED=True` دون نقل كلمة المرور إلى GitHub أو سطر الأوامر.
لذلك يجب أن يبقى `REDIS_PASSWORD` موجودًا وصحيحًا في ملف الإنتاج المحمي.

`GITHUB_TOKEN` يُوفَّر تلقائياً ولا يحتاج إعداداً.

أو من سطر الأوامر:

```bash
gh secret set DEPLOY_SSH_KEY --env production < ~/.ssh/school_reports_deploy
ssh-keyscan -H <SERVER_IP> | gh secret set DEPLOY_KNOWN_HOSTS --env production
gh secret set DEPLOY_SSH_HOST --env production --body '<SERVER_IP>'
gh secret set DEPLOY_SSH_USER --env production --body 'deploy'
```

للتأكد أن الأسماء وصلت (القيم لا تُقرأ أبداً، الأسماء فقط):

```bash
gh secret list --env production
```

#### لماذا يفشل النشر مبكراً

أول ثلاث خطوات في وظيفة النشر — التحقق من الأسرار، تهيئة SSH، اختبار الوصول —
تسبق بناء الصورة ودفعها عمداً. لو تأخّرت لما بعد الدفع، لكان سرٌّ ناقص يعني أن
وسم `latest` انتقل إلى صورة لم تصل الخادم قط، فيصير الوسم يشير إلى ما لا يعمل.
الفشل قبل الدفع يترك الإنتاج والسجلّ على حالهما تماماً.

#### تحذيرات المحرر

إضافة GitHub Actions في VSCode تُظهر `Context access might be invalid` لكل
`secrets.*`، و`Value 'production' is not valid` لاسم البيئة. كلاهما ناتج عن أن
الإضافة غير مسجّلة الدخول فلا تستطيع قراءة إعدادات المستودع. الحل:
`GitHub Actions: Sign in` من لوحة الأوامر. لا علاقة لهما بصحة الملف.

### 3. متطلبات الخادم

- Docker مع إضافة `compose` v2.
- `deploy/hetzner/env.production` موجود داخل `DEPLOY_PATH` — **لا يُرفع أبداً من CI** وهو
  الملف الوحيد الذي لا يلمسه النشر. أنشئه مرة واحدة من `env.production.example`.
- مستخدم SSH عضو في مجموعة `docker`.

للتحقق من الحالة الحالية قبل أول نشر:

```bash
ssh <USER>@<SERVER_IP> \
  'docker compose version; docker ps --format "{{.Names}}\t{{.Image}}"; \
   ls -d /opt/school_reports /srv/school_reports ~/school_reports 2>/dev/null'
```

المسار الذي يظهر فيه المشروع هو ما تضعه في `DEPLOY_PATH`.

## التراجع (rollback)

الصور موسومة بـ commit SHA، فالرجوع لأي إصدار سابق:

```bash
cd /opt/school_reports
APP_IMAGE=ghcr.io/<owner>/school_reports:<older-sha> bash deploy/hetzner/remote_deploy.sh
```

## نشر يدوي / خارج المسار

نفس السكربت يعمل يدوياً على الخادم بأي وسم صورة، وهو نفسه المستخدَم من CI —
فلا يوجد مسار نشر ثانٍ يمكن أن ينحرف عن الأول.
