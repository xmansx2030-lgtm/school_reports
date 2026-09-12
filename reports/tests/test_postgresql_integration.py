from __future__ import annotations

from unittest import skipUnless

from django.db import IntegrityError, connection, transaction
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from reports.models import GeneratedExportJob, School
from reports.services_generated_exports import locked_generated_export_job


@skipUnless(connection.vendor == "postgresql", "PostgreSQL integration only")
class PostgreSQLBehaviorTests(TransactionTestCase):
    """Backend contracts SQLite cannot characterize accurately."""

    reset_sequences = True

    def setUp(self) -> None:
        self.school = School.objects.create(
            name="PostgreSQL integration school",
            code="postgres-integration",
        )

    def test_json_field_lookup_and_timezone_round_trip(self) -> None:
        job = GeneratedExportJob.objects.create(
            school=self.school,
            kind=GeneratedExportJob.Kind.SCHOOL_ZIP,
            parameters={"source": "integration", "nested": {"version": 2}},
        )

        loaded = GeneratedExportJob.objects.get(parameters__source="integration")

        self.assertEqual(loaded.pk, job.pk)
        self.assertEqual(loaded.parameters["nested"]["version"], 2)
        self.assertTrue(timezone.is_aware(loaded.created_at))

    def test_generated_export_lock_emits_for_update_without_nullable_join(self) -> None:
        job = GeneratedExportJob.objects.create(
            school=self.school,
            kind=GeneratedExportJob.Kind.SCHOOL_ZIP,
        )

        with transaction.atomic(), CaptureQueriesContext(connection) as captured:
            locked = locked_generated_export_job(job.pk)

        self.assertIsNotNone(locked)
        self.assertEqual(locked.pk, job.pk)
        sql = " ".join(query["sql"] for query in captured.captured_queries).upper()
        self.assertIn("FOR UPDATE", sql)
        self.assertNotIn(" JOIN ", sql)

    def test_unique_school_code_is_enforced_by_the_database(self) -> None:
        with self.assertRaises(IntegrityError), transaction.atomic():
            School.objects.create(
                name="Different school",
                code=self.school.code,
            )

    def test_transaction_rolls_back_all_rows_on_failure(self) -> None:
        with self.assertRaises(RuntimeError), transaction.atomic():
            School.objects.create(name="Rollback school", code="rollback-school")
            raise RuntimeError("force rollback")

        self.assertFalse(School.objects.filter(code="rollback-school").exists())

    def test_declared_generated_export_indexes_exist(self) -> None:
        table = GeneratedExportJob._meta.db_table
        with connection.cursor() as cursor:
            constraints = connection.introspection.get_constraints(cursor, table)

        for index_name in (
            "reports_gej_user_status_idx",
            "reports_gej_school_kind_idx",
        ):
            self.assertIn(index_name, constraints)
            self.assertTrue(constraints[index_name]["index"])
