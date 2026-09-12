from __future__ import annotations

from unittest.mock import patch

from django.db import DatabaseError
from django.test import TestCase
from django.urls import reverse

from reports.models import Teacher


class ApiTenantBoundaryTests(TestCase):
    def setUp(self) -> None:
        self.user = Teacher.objects.create_user(
            phone="0500000987",
            name="API boundary user",
            password="test-password",
        )
        self.client.force_login(self.user)

    @patch("reports.views.api._get_active_school", return_value=None)
    @patch(
        "reports.views.api.School.objects.filter",
        side_effect=DatabaseError("school state unavailable"),
    )
    def test_department_members_fails_closed_when_school_state_is_unavailable(
        self,
        _school_filter,
        _active_school,
    ) -> None:
        response = self.client.get(
            reverse("reports:api_department_members"),
            {"department": "administration"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "active_school_required")
