"""Safe lifecycle cleanup for FileField/ImageField objects.

Django deliberately leaves uploaded objects in storage when a model is deleted
or a file is replaced. This module closes that gap for both local storage and
Cloudflare R2:

* capture old object names before an update/delete;
* wait until the database transaction commits;
* verify no model still references the same physical object;
* delete through the field's configured storage backend;
* queue a retrying Celery task if the storage API is temporarily unavailable.
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from collections.abc import Iterable
from contextvars import ContextVar

from django.apps import apps
from django.db import transaction
from django.db.models import FileField
from django.db.models.signals import post_delete, post_save, pre_delete, pre_save

from .task_dispatch import run_named_task_safe
from .task_names import DELETE_ORPHANED_STORAGE_FILE_TASK

logger = logging.getLogger(__name__)

_connected = False
_cleanup_suppressed = ContextVar("reports_file_cleanup_suppressed", default=False)


@contextmanager
def suppress_file_cleanup():
    """Temporarily keep physical files for an explicit maintenance workflow."""
    token = _cleanup_suppressed.set(True)
    try:
        yield
    finally:
        _cleanup_suppressed.reset(token)


def _model_file_fields(model) -> list[FileField]:
    return [
        field
        for field in model._meta.concrete_fields
        if isinstance(field, FileField)
    ]


def _all_file_fields() -> Iterable[tuple[type, FileField]]:
    for app_config in apps.get_app_configs():
        for model in app_config.get_models():
            for field in _model_file_fields(model):
                yield model, field


def _storage_signature(storage) -> tuple:
    """Identify the physical storage namespace, not merely the Python class."""
    bucket = getattr(storage, "bucket_name", None)
    if bucket:
        endpoint = getattr(storage, "endpoint_url", None) or ""
        location = getattr(storage, "location", None) or ""
        return ("object-storage", str(endpoint), str(bucket), str(location))

    location = getattr(storage, "location", None)
    if location:
        return ("filesystem", os.path.abspath(str(location)))

    cls = type(storage)
    return (
        "storage-class",
        cls.__module__,
        cls.__qualname__,
        str(getattr(storage, "base_url", "") or ""),
    )


def _file_name(instance, field: FileField) -> str:
    try:
        value = getattr(instance, field.name)
        return (getattr(value, "name", "") or "").strip()
    except Exception:
        return ""


def _is_still_referenced(*, name: str, storage) -> bool:
    signature = _storage_signature(storage)
    for model, field in _all_file_fields():
        try:
            if _storage_signature(field.storage) != signature:
                continue
            if model._default_manager.filter(**{field.name: name}).exists():
                return True
        except Exception:
            # Be conservative: a database/schema problem must never cause
            # irreversible deletion of an object whose references are unknown.
            logger.exception(
                "file_cleanup: reference check failed model=%s field=%s name=%s",
                model._meta.label,
                field.name,
                name,
            )
            return True
    return False


def delete_file_if_unreferenced(
    model_label: str,
    field_name: str,
    name: str,
) -> bool:
    """Delete one storage object if no FileField still points to it."""
    name = (name or "").strip()
    if not name:
        return False

    model = apps.get_model(model_label)
    field = model._meta.get_field(field_name)
    if not isinstance(field, FileField):
        return False
    storage = field.storage
    if _is_still_referenced(name=name, storage=storage):
        logger.info(
            "file_cleanup: kept referenced object model=%s field=%s name=%s",
            model_label,
            field_name,
            name,
        )
        return False

    storage.delete(name)
    logger.info(
        "file_cleanup: deleted storage object model=%s field=%s name=%s",
        model_label,
        field_name,
        name,
    )
    return True


def _delete_after_commit(model_label: str, field_name: str, name: str) -> None:
    try:
        delete_file_if_unreferenced(model_label, field_name, name)
    except Exception:
        logger.exception(
            "file_cleanup: immediate delete failed; scheduling retry "
            "model=%s field=%s name=%s",
            model_label,
            field_name,
            name,
        )
        try:
            run_named_task_safe(
                DELETE_ORPHANED_STORAGE_FILE_TASK,
                delete_file_if_unreferenced,
                model_label,
                field_name,
                name,
            )
        except Exception:
            logger.exception(
                "file_cleanup: could not schedule retry model=%s field=%s name=%s",
                model_label,
                field_name,
                name,
            )


def _schedule(candidates: Iterable[tuple[str, str, str]]) -> None:
    for model_label, field_name, name in set(candidates):
        if not name:
            continue
        transaction.on_commit(
            lambda ml=model_label, fn=field_name, n=name: _delete_after_commit(
                ml,
                fn,
                n,
            )
        )


def _connect_model(model) -> None:
    fields = _model_file_fields(model)
    if not fields:
        return
    label = model._meta.label

    def capture_replaced_files(sender, instance, **kwargs):
        if kwargs.get("raw") or not getattr(instance, "pk", None):
            instance._file_cleanup_replaced = []
            return

        update_fields = kwargs.get("update_fields")
        allowed_fields = set(update_fields) if update_fields is not None else None
        try:
            old = sender._default_manager.filter(pk=instance.pk).first()
        except Exception:
            old = None
        candidates = []
        if old is not None:
            for field in fields:
                if allowed_fields is not None and field.name not in allowed_fields:
                    continue
                old_name = _file_name(old, field)
                new_name = _file_name(instance, field)
                if old_name and old_name != new_name:
                    candidates.append((label, field.name, old_name))
        instance._file_cleanup_replaced = candidates

    def schedule_replaced_files(sender, instance, **kwargs):
        if kwargs.get("raw") or _cleanup_suppressed.get():
            return
        _schedule(getattr(instance, "_file_cleanup_replaced", []))

    def capture_deleted_files(sender, instance, **kwargs):
        instance._file_cleanup_deleted = [
            (label, field.name, name)
            for field in fields
            if (name := _file_name(instance, field))
        ]

    def schedule_deleted_files(sender, instance, **kwargs):
        if _cleanup_suppressed.get():
            return
        _schedule(getattr(instance, "_file_cleanup_deleted", []))

    pre_save.connect(
        capture_replaced_files,
        sender=model,
        weak=False,
        dispatch_uid=f"reports.file_cleanup.pre_save.{label}",
    )
    post_save.connect(
        schedule_replaced_files,
        sender=model,
        weak=False,
        dispatch_uid=f"reports.file_cleanup.post_save.{label}",
    )
    pre_delete.connect(
        capture_deleted_files,
        sender=model,
        weak=False,
        dispatch_uid=f"reports.file_cleanup.pre_delete.{label}",
    )
    post_delete.connect(
        schedule_deleted_files,
        sender=model,
        weak=False,
        dispatch_uid=f"reports.file_cleanup.post_delete.{label}",
    )


def connect_all() -> None:
    """Connect every concrete FileField in installed apps, including future ones."""
    global _connected
    if _connected:
        return
    for app_config in apps.get_app_configs():
        for model in app_config.get_models():
            _connect_model(model)
    _connected = True
