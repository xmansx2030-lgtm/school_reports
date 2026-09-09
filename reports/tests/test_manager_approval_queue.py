from django.test import TestCase
from django.utils import timezone

from reports.manager_approval_queue import manager_approval_rows
from reports.model_parts.approvals import ApprovalState
from reports.models import (
    Assignment,
    AssignmentTarget,
    CircularDraft,
    Department,
    Document,
    Initiative,
    LabExperiment,
    Meeting,
    MeetingMinutes,
    Plan,
    Report,
    ReportType,
    School,
    SchoolMembership,
    SchoolSubscription,
    SubscriptionPlan,
    Teacher,
    TeacherAchievementFile,
)


class ManagerApprovalQueueTests(TestCase):
    """One decision queue covers every school workflow that needs approval."""

    def setUp(self):
        self.school = School.objects.create(
            name="مدرسة صندوق القرار",
            code="manager-approval-queue",
            current_academic_year="1447-1448",
        )
        self.department = Department.objects.create(
            school=self.school,
            name="قسم الجودة",
            slug="quality-queue",
        )
        subscription_plan = SubscriptionPlan.objects.create(
            name="باقة صندوق القرار",
            price=0,
            days_duration=365,
            max_teachers=10,
        )
        SchoolSubscription.objects.create(
            school=self.school,
            plan=subscription_plan,
        )
        self.manager = Teacher.objects.create_user(
            phone="500190001",
            name="مدير الصندوق",
            password="x",
            is_staff=True,
        )
        self.teacher = Teacher.objects.create_user(
            phone="500190002",
            name="منسوب المدرسة",
            password="x",
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.manager,
            role_type=SchoolMembership.RoleType.MANAGER,
        )
        SchoolMembership.objects.create(
            school=self.school,
            teacher=self.teacher,
            role_type=SchoolMembership.RoleType.TEACHER,
        )
        self.report_type = ReportType.objects.create(
            school=self.school,
            code="queue-report",
            name="تقرير الصندوق",
        )

    def _pending(self) -> dict:
        return {
            "approval_state": ApprovalState.SUBMITTED,
            "submitted_at": timezone.now(),
        }

    def test_every_manager_approval_workflow_is_in_the_same_queue(self):
        pending = self._pending()
        Report.objects.create(
            school=self.school,
            teacher=self.teacher,
            teacher_name=self.teacher.name,
            title="تقرير معلّق",
            report_date=timezone.localdate(),
            academic_year="1447-1448",
            category=self.report_type,
            **pending,
        )
        Document.objects.create(
            school=self.school,
            uploaded_by=self.teacher,
            owner=self.teacher,
            title="وثيقة معلّقة",
            academic_year="1447-1448",
            file="documents/pending.pdf",
            **pending,
        )
        assignment = Assignment.objects.create(
            school=self.school,
            issuer=self.manager,
            title="تكليف معلّق",
            due_at=timezone.now(),
        )
        AssignmentTarget.objects.create(
            assignment=assignment,
            assignee=self.teacher,
            school=self.school,
            progress_percent=100,
            **pending,
        )
        Plan.objects.create(
            school=self.school,
            owner=self.teacher,
            title="خطة معلّقة",
            academic_year="1447-1448",
            **pending,
        )
        meeting = Meeting.objects.create(
            school=self.school,
            organizer=self.manager,
            title="اجتماع معلّق محضره",
            scheduled_at=timezone.now(),
            status=Meeting.Status.HELD,
        )
        MeetingMinutes.objects.create(
            meeting=meeting,
            recorder=self.teacher,
            body="محضر مكتمل للمراجعة.",
            **pending,
        )
        Initiative.objects.create(
            school=self.school,
            teacher=self.teacher,
            title="مبادرة معلّقة",
            summary="وصف المبادرة وأثرها.",
            **pending,
        )
        CircularDraft.objects.create(
            school=self.school,
            owner=self.teacher,
            title="تعميم معلّق",
            body="نص التعميم.",
            **pending,
        )
        LabExperiment.objects.create(
            school=self.school,
            department=self.department,
            lab_kind="science",
            recorder=self.teacher,
            title="تجربة معلّقة",
            experiment_date=timezone.localdate(),
            procedure="خطوات التجربة.",
            **pending,
        )
        TeacherAchievementFile.objects.create(
            school=self.school,
            teacher=self.teacher,
            academic_year="1447-1448",
            status=TeacherAchievementFile.Status.SUBMITTED,
            submitted_at=timezone.now(),
        )

        rows = manager_approval_rows(self.manager, self.school)

        self.assertEqual(
            {row["kind"] for row in rows},
            {
                "report",
                "document",
                "assignment",
                "plan",
                "minutes",
                "initiative",
                "circular",
                "experiment",
                "achievement",
            },
        )
        self.assertTrue(all(row["is_mine"] for row in rows))
        self.assertTrue(all(row["detail_url"] for row in rows))

    def test_a_manager_does_not_receive_their_own_achievement_for_self_approval(self):
        TeacherAchievementFile.objects.create(
            school=self.school,
            teacher=self.manager,
            academic_year="1447-1448",
            status=TeacherAchievementFile.Status.SUBMITTED,
            submitted_at=timezone.now(),
        )

        self.assertEqual(manager_approval_rows(self.manager, self.school), [])
