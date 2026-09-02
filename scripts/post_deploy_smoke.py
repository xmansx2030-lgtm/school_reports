#!/usr/bin/env python3
"""فحص ما بعد النشر — على الموقع الحيّ، من الخارج.

**لماذا لا يكفي ``production_preflight``؟** لأنه يقرأ إعدادات العملية من
داخلها، فلا يرى ما يفعله ما هو أمامها. وترويسات الحماية تحديداً يكتبها Caddy،
و``SecurityMiddleware`` في Django لا يستبدل ترويسةً موجودة — فكان
``SECURE_HSTS_PRELOAD = True`` مضبوطاً في الإعدادات وغائباً عن الاستجابة، ولا
فحصٍ داخلي يمكنه أن يكشف ذلك. الرد الحقيقي وحده يفصل.

**آمن على الإنتاج:** طلبات ``GET``/``HEAD`` على صفحات عامة فقط. لا كتابة، لا
مصادقة، لا حِمل يُذكر.

    python scripts/post_deploy_smoke.py https://tawtheeq-ksa.com

يخرج بـ 1 عند أي إخفاق، فيصلح بوّابةً في خطوة نشر.
"""
from __future__ import annotations

import sys
import urllib.error
import urllib.request

# متصفح حقيقي: الأصل خلف Cloudflare، وعميلٌ آلي بلا ترويسة متصفح يستقبل صفحة
# تحدٍّ بترويسات Cloudflare لا بترويسات التطبيق — فيُفحص الشيء الخطأ ويمرّ.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"
results: list[tuple[str, str, str]] = []


def record(level: str, title: str, detail: str = "") -> None:
    results.append((level, title, detail))


def fetch(url: str) -> tuple[int, dict[str, str], bytes]:
    # المخطَّط يُفرض هنا لا يُفترض: العنوان يأتي من سطر الأوامر، و``file:`` أو
    # مخطَّط مخصَّص يحوّل فحص موقعٍ عام إلى قراءة قرص.
    if not url.startswith(("https://", "http://")):
        raise ValueError(f"Refusing a non-HTTP(S) target: {url!r}")
    request = urllib.request.Request(  # noqa: S310 - المخطَّط مُتحقَّق منه أعلاه
        url, headers={"User-Agent": BROWSER_UA, "Accept": "text/html"}
    )
    try:
        with urllib.request.urlopen(request, timeout=25) as response:  # noqa: S310
            headers = {k.lower(): v for k, v in response.headers.items()}
            return int(response.status), headers, response.read(200_000)
    except urllib.error.HTTPError as exc:
        headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
        return int(exc.code), headers, b""


def check_headers(base: str) -> None:
    status, headers, _ = fetch(base + "/")

    if status != 200:
        record(FAIL, f"Landing page returned {status}", "Expected 200.")
        return
    record(PASS, "Landing page reachable")

    if "cf-mitigated" in headers:
        record(
            WARN,
            "Cloudflare answered with a challenge",
            "The headers below are Cloudflare's, not the application's.",
        )

    hsts = headers.get("strict-transport-security", "")
    if "preload" in hsts and "includesubdomains" in hsts.lower():
        record(PASS, f"HSTS: {hsts}")
    elif hsts:
        record(
            FAIL,
            f"HSTS is incomplete: {hsts}",
            "Django's SECURE_HSTS_PRELOAD has no effect when Caddy writes this "
            "header first. Fix it in deploy/hetzner/Caddyfile.fragment.",
        )
    else:
        record(FAIL, "HSTS header is missing")

    required = {
        "content-security-policy": ("frame-ancestors 'none'", "object-src 'none'"),
        "x-content-type-options": ("nosniff",),
        "x-frame-options": ("DENY",),
        "referrer-policy": ("strict-origin",),
        "cross-origin-opener-policy": ("same-origin",),
        "permissions-policy": ("camera=()", "microphone=(self)"),
        "cross-origin-resource-policy": ("same-origin",),
    }
    for name, needles in required.items():
        value = headers.get(name, "")
        if not value:
            record(FAIL, f"{name} is missing")
            continue
        missing = [n for n in needles if n.lower() not in value.lower()]
        if missing:
            record(FAIL, f"{name} lacks {missing}", value[:120])
        else:
            record(PASS, f"{name} present")

    csp = headers.get("content-security-policy", "")
    script_src = csp.split("style-src")[0]
    for cdn in ("cdn.jsdelivr.net", "unpkg.com"):
        if cdn in script_src:
            record(FAIL, f"{cdn} is an allowed script source", "It serves any package.")
    if "'unsafe-eval'" in csp:
        record(FAIL, "CSP allows 'unsafe-eval'")

    cache_control = headers.get("cache-control", "")
    if "no-store" in cache_control or "private" in cache_control:
        record(PASS, f"Landing page is not shared-cacheable: {cache_control}")
    else:
        record(
            FAIL,
            f"Landing page cache policy is unsafe: {cache_control or '(none)'}",
            "A shared cache could serve one tenant's response to another.",
        )


def check_no_indexing_of_private_pages(base: str) -> None:
    status, headers, _ = fetch(base + "/login/")
    if status not in (200, 302):
        record(WARN, f"/login/ returned {status}")
        return
    robots = headers.get("x-robots-tag", "")
    if "noindex" in robots:
        record(PASS, f"/login/ is noindex ({robots})")
    else:
        record(FAIL, "/login/ is missing X-Robots-Tag: noindex")


def check_health(base: str) -> None:
    status, _, body = fetch(base + "/healthz/")
    if status == 200:
        record(PASS, "Health endpoint is up")
    else:
        record(FAIL, f"Health endpoint returned {status}", body[:120].decode("utf-8", "replace"))


def check_secrets_not_exposed(base: str) -> None:
    """لا يجوز أن يُخدَم ملف بيئة ولا نسخة قاعدة بيانات على العلن."""
    for path in ("/.env", "/db.sqlite3", "/.git/config"):
        status, _, _ = fetch(base + path)
        if status in (200, 206):
            record(FAIL, f"{path} is publicly served")
        else:
            record(PASS, f"{path} is not served ({status})")


def main() -> int:
    base = (sys.argv[1] if len(sys.argv) > 1 else "https://tawtheeq-ksa.com").rstrip("/")
    print(f"Post-deploy smoke test against {base}\n")

    for check in (
        check_health,
        check_headers,
        check_no_indexing_of_private_pages,
        check_secrets_not_exposed,
    ):
        try:
            check(base)
        except Exception as exc:  # noqa: BLE001 - فشل الفحص نفسه نتيجةٌ أيضاً
            record(FAIL, f"{check.__name__} could not run", str(exc)[:160])

    failures = 0
    for level, title, detail in results:
        marker = {PASS: "  ok  ", FAIL: " FAIL ", WARN: " warn "}[level]
        print(f"[{marker}] {title}")
        if detail:
            print(f"           {detail}")
        failures += level == FAIL

    print()
    if failures:
        print(f"{failures} check(s) failed.")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
