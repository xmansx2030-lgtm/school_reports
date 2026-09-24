# config/urls.py
from django.contrib import admin
from django.urls import path, include, reverse
from django.conf import settings
from django.conf.urls.static import static as serve_static
from django.contrib.staticfiles import finders
from django.contrib.staticfiles.storage import staticfiles_storage
from datetime import timedelta, timezone as dt_timezone

from django.http import HttpResponse
from django.utils import timezone
from django.utils.html import escape
from django.views.decorators.cache import cache_control
from django.views.generic.base import RedirectView


admin.site.site_header = "إدارة منصة توثيق"
admin.site.site_title = "إدارة منصة توثيق"
admin.site.index_title = "لوحة تشغيل المنصة"


@cache_control(no_cache=True, must_revalidate=True, max_age=0)
def service_worker(request):
    """Serve service worker from site root to allow scope="/"."""
    content = None
    # Production/collected static
    try:
        with staticfiles_storage.open("sw.js") as fp:
            content = fp.read()
    except Exception:
        content = None

    # Development fallback (no collectstatic): resolve from STATICFILES_DIRS / app static
    if content is None:
        path = finders.find("sw.js")
        if path:
            with open(path, "rb") as fp:
                content = fp.read()

    if content is None:
        return HttpResponse("Service worker not found.", status=404, content_type="text/plain")

    response = HttpResponse(content, content_type="application/javascript")
    response["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    # Keep intermediary/CDN caches from pinning an old worker after deployment.
    response["CDN-Cache-Control"] = "no-store"
    response["Cloudflare-CDN-Cache-Control"] = "no-store"
    response["Service-Worker-Allowed"] = "/"
    return response


def _public_site_url(request):
    configured = str(getattr(settings, "SITE_URL", "") or "").strip().rstrip("/")
    return configured or request.build_absolute_uri("/").rstrip("/")


def robots_txt(request):
    base = _public_site_url(request)
    content = "\n".join([
        "User-agent: *",
        "Allow: /",
        "Disallow: /admin-panel/",
        "Disallow: /api/",
        "Disallow: /dashboard/",
        "Disallow: /media/",
        "Disallow: /ops/",
        f"Sitemap: {base}{reverse('sitemap_xml')}",
        "",
    ])
    response = HttpResponse(content, content_type="text/plain; charset=utf-8")
    response["Cache-Control"] = "public, max-age=3600"
    return response


def security_txt(request):
    base = request.build_absolute_uri("/").rstrip("/")
    contact_email = (
        str(getattr(settings, "SECURITY_CONTACT_EMAIL", "support@tawtheeq-ksa.com"))
        .replace("\r", "")
        .replace("\n", "")
        .strip()
    )
    # RFC 9116 يوجب حقل Expires، ويوصي بمدة أقل من سنة.
    expires = (timezone.now() + timedelta(days=180)).astimezone(dt_timezone.utc).replace(microsecond=0)
    content = "\n".join([
        f"Contact: mailto:{contact_email}",
        f"Expires: {expires.strftime('%Y-%m-%dT%H:%M:%SZ')}",
        f"Canonical: {base}/.well-known/security.txt",
        f"Policy: {request.build_absolute_uri(reverse('reports:privacy_policy'))}",
        "Preferred-Languages: ar, en",
        "",
    ])
    response = HttpResponse(content, content_type="text/plain")
    response["Cache-Control"] = "public, max-age=3600"
    return response


def sitemap_xml(request):
    routes = (
        ("reports:landing", "weekly", "1.0"),
        ("reports:faq", "monthly", "0.8"),
        ("reports:user_guide", "monthly", "0.8"),
        ("reports:privacy_policy", "yearly", "0.3"),
        ("reports:terms_conditions", "yearly", "0.3"),
        ("reports:refund_policy", "yearly", "0.3"),
        ("reports:service_delivery_policy", "yearly", "0.3"),
        ("reports:complaints_policy", "yearly", "0.4"),
    )
    base = _public_site_url(request)
    body = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for route_name, changefreq, priority in routes:
        url = escape(f"{base}{reverse(route_name)}")
        body.extend([
            "  <url>",
            f"    <loc>{url}</loc>",
            f"    <changefreq>{changefreq}</changefreq>",
            f"    <priority>{priority}</priority>",
            "  </url>",
        ])
    body.append("</urlset>")
    response = HttpResponse("\n".join(body), content_type="application/xml; charset=utf-8")
    response["Cache-Control"] = "public, max-age=3600"
    return response

from core.views import healthz, ops_metrics

urlpatterns = [
    # Health check — lightweight, no auth/session required
    path("healthz/", healthz, name="healthz"),
    # Operational metrics — superuser only
    path("ops/metrics/", ops_metrics, name="ops_metrics"),
    path("admin-panel/", admin.site.urls),
    path(
        "favicon.ico",
        RedirectView.as_view(url="/static/favicon.ico", permanent=True),
    ),
    path(
        "favicon.png",
        RedirectView.as_view(url="/static/favicon.ico", permanent=False),
    ),
    path(
        "touch-icon-iphone.png",
        RedirectView.as_view(url="/static/img/pwa/apple-touch-icon-180.png", permanent=False),
    ),
    # iOS Safari may request these from site root regardless of <link rel="apple-touch-icon">.
    path(
        "apple-touch-icon.png",
        RedirectView.as_view(url="/static/img/pwa/apple-touch-icon-180.png", permanent=False),
    ),
    path(
        "apple-touch-icon-precomposed.png",
        RedirectView.as_view(url="/static/img/pwa/apple-touch-icon-180.png", permanent=False),
    ),
    path(
        "apple-touch-icon-120x120.png",
        RedirectView.as_view(url="/static/img/pwa/apple-touch-icon-180.png", permanent=False),
    ),
    path(
        "apple-touch-icon-120x120-precomposed.png",
        RedirectView.as_view(url="/static/img/pwa/apple-touch-icon-180.png", permanent=False),
    ),
    path(
        "apple-touch-icon-152x152.png",
        RedirectView.as_view(url="/static/img/pwa/apple-touch-icon-180.png", permanent=False),
    ),
    path(
        "apple-touch-icon-152x152-precomposed.png",
        RedirectView.as_view(url="/static/img/pwa/apple-touch-icon-180.png", permanent=False),
    ),
    path(
        "apple-touch-icon-167x167.png",
        RedirectView.as_view(url="/static/img/pwa/apple-touch-icon-180.png", permanent=False),
    ),
    path(
        "apple-touch-icon-167x167-precomposed.png",
        RedirectView.as_view(url="/static/img/pwa/apple-touch-icon-180.png", permanent=False),
    ),
    path(
        "apple-touch-icon-180x180.png",
        RedirectView.as_view(url="/static/img/pwa/apple-touch-icon-180.png", permanent=False),
    ),
    path(
        "apple-touch-icon-180x180-precomposed.png",
        RedirectView.as_view(url="/static/img/pwa/apple-touch-icon-180.png", permanent=False),
    ),
    path("robots.txt", robots_txt),
    path("sitemap.xml", sitemap_xml, name="sitemap_xml"),
    path(".well-known/security.txt", security_txt, name="security_txt"),
    path("sw.js", service_worker, name="service_worker"),
    # REST API v1
    path("api/v1/", include("reports.api_urls")),
    path("api/operations/v1/", include("operations.urls")),
    path("dashboard/system/", include("maintenance.urls")),
    path("personal/", include("personal.urls")),
    path("", include("reports.urls")),  # واجهة المشروع الأساسية
]

# ✅ أثناء التطوير فقط: نخدم الملفات الثابتة والوسائط من Django مباشرة
if settings.DEBUG:
    urlpatterns += serve_static(settings.STATIC_URL, document_root=settings.BASE_DIR / "static")
    urlpatterns += serve_static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
