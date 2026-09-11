# reports/serializers.py
# -*- coding: utf-8 -*-
"""
DRF serializers for the reports app.
Provides a read-only API foundation for mobile/third-party integration.
"""
from __future__ import annotations

from rest_framework import serializers

from .report_limits import REPORT_DETAILS_MAX_LENGTH

from .models import (
    Notification,
    Report,
    ReportType,
    School,
    Teacher,
    Ticket,
)


class SchoolSerializer(serializers.ModelSerializer):
    class Meta:
        model = School
        fields = ["id", "name", "code", "gender", "is_active"]
        read_only_fields = fields


class TeacherSerializer(serializers.ModelSerializer):
    class Meta:
        model = Teacher
        fields = ["id", "name", "phone", "is_active"]
        read_only_fields = fields


class ReportTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReportType
        fields = ["id", "name", "is_active"]
        read_only_fields = fields


class ReportListSerializer(serializers.ModelSerializer):
    teacher_name = serializers.CharField(read_only=True)
    category_name = serializers.CharField(source="category.name", default=None, read_only=True)

    class Meta:
        model = Report
        fields = [
            "id", "title", "report_date", "teacher_name",
            "category_name", "created_at",
        ]
        read_only_fields = fields


class TicketListSerializer(serializers.ModelSerializer):
    creator_name = serializers.CharField(source="creator.name", default="", read_only=True)

    class Meta:
        model = Ticket
        fields = [
            "id", "title", "status",
            "creator_name", "created_at",
        ]
        read_only_fields = fields


class NotificationListSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = [
            "id", "title", "message", "is_important",
            "created_at",
        ]
        read_only_fields = fields


class ReportCreateSerializer(serializers.ModelSerializer):
    """إنشاء تقرير عبر الـAPI.

    **المدرسة والمُعِدّ لا يُقبلان من الحمولة.** كلاهما يُشتقّ من سياق الطلب:
    المدرسة من المفتاح أو الجلسة، والمُعِدّ من الهوية المصادَق بها. وقبولُهما
    من العميل كان يعني أن مفتاح مدرسةٍ يكتب في مدرسةٍ أخرى بتغيير رقم في JSON
    — وهو أسهل اختراقٍ ممكن لعزل المستأجرين.

    ``category`` يُقيَّد بأنواع هذه المدرسة وحدها للسبب نفسه.
    """

    idea = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        max_length=REPORT_DETAILS_MAX_LENGTH,
    )

    class Meta:
        model = Report
        fields = [
            "id", "title", "report_date", "category", "idea",
            "goal", "implementation_method", "results", "recommendations",
            "beneficiaries_count", "academic_year",
        ]
        read_only_fields = ["id"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        school = self.context.get("school")
        field = self.fields.get("category")
        if field is not None:
            from .models import ReportType

            field.queryset = ReportType.objects.filter(
                is_active=True
            ).filter(school=school) if school is not None else ReportType.objects.none()

    def create(self, validated_data):
        validated_data["school"] = self.context["school"]
        validated_data["teacher"] = self.context["teacher"]
        return super().create(validated_data)
