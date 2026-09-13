"""Role-oriented navigation helpers for the platform administration UI.

Django groups models by Python application. That is useful to developers, but
the platform team works by business task. These helpers keep every permitted
model reachable while presenting common work first and technical tools in a
separate, collapsed area.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from django import template

register = template.Library()

ModelKey = tuple[str, str]
SectionDefinition = tuple[str, str, tuple[ModelKey, ...]]


PRIMARY_SECTIONS: tuple[SectionDefinition, ...] = (
    (
        "المدارس والمستخدمون",
        "الحسابات، المدارس، العضويات، ومجموعات المدارس.",
        (
            ("reports", "teacher"),
            ("reports", "school"),
            ("reports", "schoolmembership"),
            ("reports", "schooladditionrequest"),
            ("reports", "schoolgroup"),
            ("reports", "schoolgroupmembership"),
        ),
    ),
    (
        "الاشتراكات والإيرادات",
        "الباقات، اشتراكات المدارس، المدفوعات، وأكواد الخصم.",
        (
            ("reports", "schoolsubscription"),
            ("reports", "subscriptionplan"),
            ("reports", "payment"),
            ("reports", "discountcode"),
            ("reports", "discountredemption"),
        ),
    ),
    (
        "التقارير والمحتوى",
        "التقارير، أنواعها، الأقسام، والسنوات الدراسية.",
        (
            ("reports", "report"),
            ("reports", "reporttype"),
            ("reports", "department"),
            ("reports", "academicyear"),
        ),
    ),
    (
        "الدعم والتواصل",
        "طلبات الدعم، شكاوى العملاء، والإشعارات الرسمية.",
        (
            ("reports", "ticket"),
            ("reports", "customercomplaint"),
            ("reports", "notification"),
            ("reports", "erasurerequest"),
        ),
    ),
    (
        "إدارة المنصة",
        "الإعدادات العامة وأرشيف السنوات الدراسية.",
        (
            ("reports", "platformsettings"),
            ("reports", "schoolyeararchive"),
        ),
    ),
)


ADVANCED_SECTIONS: tuple[SectionDefinition, ...] = (
    (
        "الأرشفة والتخزين",
        "ملحقات الأرشفة، التنزيلات، وعمليات تهيئة العام.",
        (
            ("reports", "schoolarchiveaddon"),
            ("reports", "schoolyeararchivedownload"),
            ("reports", "archivestorageoption"),
            ("maintenance", "schoolyearresetjob"),
        ),
    ),
    (
        "الأمان والتسليمات",
        "أجهزة الدخول، مفاتيح التكامل، وتفاصيل تسليم الإشعارات.",
        (
            ("reports", "teachertotpdevice"),
            ("reports", "webauthncredential"),
            ("reports", "schoolapikey"),
            ("reports", "notificationrecipient"),
            ("reports", "webpushsubscription"),
            ("reports", "webpushdelivery"),
        ),
    ),
    (
        "السجلات والتفاصيل الداخلية",
        "سجلات التدقيق والاستهلاك والتفاصيل المساندة.",
        (
            ("reports", "auditlog"),
            ("reports", "aiusageevent"),
            ("reports", "ticketnote"),
            ("auth", "group"),
        ),
    ),
    (
        "مركز عمليات الخادم",
        "الخوادم والخدمات والحوادث والقياسات التشغيلية.",
        (
            ("operations", "managedserver"),
            ("operations", "managedproject"),
            ("operations", "managedservice"),
            ("operations", "incident"),
            ("operations", "healthcheck"),
            ("operations", "operationsmembership"),
            ("operations", "mobiledevice"),
            ("operations", "mobileaccesstoken"),
            ("operations", "operationaction"),
            ("operations", "projectmetricsnapshot"),
            ("operations", "servermetricsnapshot"),
        ),
    ),
    (
        "مهام الخلفية",
        "نتائج مهام Celery للمراجعة التقنية عند الحاجة.",
        (
            ("django_celery_results", "groupresult"),
            ("django_celery_results", "taskresult"),
        ),
    ),
)


def _model_index(app_list: Iterable[dict[str, Any]] | None) -> dict[ModelKey, dict[str, Any]]:
    indexed: dict[ModelKey, dict[str, Any]] = {}
    for app in app_list or ():
        app_label = str(app.get("app_label") or "").lower()
        for model in app.get("models") or ():
            object_name = str(model.get("object_name") or "").lower()
            indexed[(app_label, object_name)] = model
    return indexed


def _build_sections(
    definitions: Iterable[SectionDefinition],
    indexed: dict[ModelKey, dict[str, Any]],
) -> tuple[list[dict[str, Any]], set[ModelKey]]:
    sections: list[dict[str, Any]] = []
    consumed: set[ModelKey] = set()
    for title, description, keys in definitions:
        models = [indexed[key] for key in keys if key in indexed]
        if not models:
            continue
        consumed.update(key for key in keys if key in indexed)
        sections.append(
            {
                "title": title,
                "description": description,
                "models": models,
                "count": len(models),
            }
        )
    return sections, consumed


@register.simple_tag
def platform_admin_navigation(app_list):
    """Return primary and advanced navigation without dropping future models."""

    indexed = _model_index(app_list)
    primary, primary_keys = _build_sections(PRIMARY_SECTIONS, indexed)
    advanced, advanced_keys = _build_sections(ADVANCED_SECTIONS, indexed)

    remaining_keys = set(indexed) - primary_keys - advanced_keys
    if remaining_keys:
        remaining_models = [
            indexed[key]
            for key in sorted(
                remaining_keys,
                key=lambda item: str(indexed[item].get("name") or ""),
            )
        ]
        advanced.append(
            {
                "title": "نماذج أخرى",
                "description": "نماذج جديدة أو داخلية لم تُصنّف بعد.",
                "models": remaining_models,
                "count": len(remaining_models),
            }
        )

    return {
        "primary": primary,
        "advanced": advanced,
        "primary_count": sum(section["count"] for section in primary),
        "advanced_count": sum(section["count"] for section in advanced),
    }
