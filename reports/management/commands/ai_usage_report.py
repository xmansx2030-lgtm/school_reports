"""تقرير استهلاك الذكاء الاصطناعي — الأسئلة الأربعة التي لم يكن لها جواب.

    python manage.py ai_usage_report --days 30
    python manage.py ai_usage_report --days 7 --schools 10
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db.models import Avg, Count, Max, Q, Sum
from django.utils import timezone

from reports.ai_usage import model_pricing
from reports.models import AiUsageEvent


def _ratio(part: int, whole: int) -> str:
    if whole <= 0:
        return "—"
    return f"{100 * part / whole:.1f}%"


def _count(n: int, one: str, two: str, few: str, many: str) -> str:
    """صيغةُ العدد العربية. المثنّى صيغةٌ قائمة بذاتها، و«6 إعادة» ليست عربية.

    ٣–١٠ جمعٌ («6 إعادات»)، وما فوقها مفردٌ منصوب («46 ردًّا»).
    """
    if n == 1:
        return one
    if n == 2:
        return two
    if 3 <= n <= 10:
        return f"{n} {few}"
    return f"{n:,} {many}"


def _money(value) -> str:
    if value is None:
        return "—"
    return f"${Decimal(value):.4f}"


class Command(BaseCommand):
    help = "ملخص استهلاك الذكاء الاصطناعي: النداءات والرموز وإصابة المخزَّن والكلفة."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=30, help="نافذة التقرير بالأيام.")
        parser.add_argument("--schools", type=int, default=5, help="عدد المدارس الأعلى استهلاكاً.")

    def handle(self, *args, **options):
        days = max(1, int(options["days"]))
        since = timezone.now() - timedelta(days=days)
        events = AiUsageEvent.objects.filter(created_at__gte=since)

        total = events.count()
        self.stdout.write(self.style.MIGRATE_HEADING(f"\nاستهلاك الذكاء الاصطناعي — آخر {days} يومًا"))
        if not total:
            self.stdout.write("لا توجد نداءات مسجَّلة في هذه النافذة.")
            if not model_pricing():
                self.stdout.write(self.style.WARNING("تنبيه: AI_MODEL_PRICING غير مضبوط، فلن تُحسب أي كلفة."))
            return

        if not model_pricing():
            self.stdout.write(
                self.style.WARNING(
                    "AI_MODEL_PRICING غير مضبوط — الرموز مسجَّلة والكلفة فارغة. "
                    "اضبطه ثم أعد التشغيل لقراءة الفاتورة."
                )
            )

        # ── حسب المرحلة ──────────────────────────────────────────
        self.stdout.write(self.style.MIGRATE_LABEL("\nحسب المرحلة"))
        # الأرقام أولاً واسمُ المرحلة آخراً بلا حشو. عمودٌ عربيٌّ بعرضٍ ثابت
        # يختلّ عند أول تسميةٍ تتجاوزه، ولأن الطرفية تخلط اتجاهي النصّ ينزاح
        # الصفُّ كلّه معه. وبإخراج التسمية من الجدول تبقى الأعمدة الرقمية
        # مصطفّةً مهما طالت التسمية.
        header = (
            f"{'نداء':>7}{'فشل':>7}{'بتر':>6}{'إدخال':>11}{'مخزَّن':>9}"
            f"{'إخراج':>10}{'تفكير':>9}{'زمن~':>9}{'كلفة':>13}  المرحلة"
        )
        self.stdout.write(header)
        self.stdout.write("-" * 81)

        rows = (
            events.values("stage")
            .annotate(
                calls=Count("id"),
                failed=Count("id", filter=Q(outcome=AiUsageEvent.Outcome.FAILED)),
                truncated=Count("id", filter=Q(outcome=AiUsageEvent.Outcome.TRUNCATED)),
                tokens_in=Sum("input_tokens"),
                cached=Sum("cached_input_tokens"),
                tokens_out=Sum("output_tokens"),
                reasoning=Sum("reasoning_tokens"),
                avg_ms=Avg("duration_ms"),
                cost=Sum("estimated_cost"),
            )
            .order_by("-calls")
        )
        labels = dict(AiUsageEvent.Stage.choices)
        for row in rows:
            self.stdout.write(
                f"{row['calls']:>7}"
                f"{row['failed']:>7}"
                f"{row['truncated']:>6}"
                f"{row['tokens_in'] or 0:>11,}"
                f"{_ratio(row['cached'] or 0, row['tokens_in'] or 0):>9}"
                f"{row['tokens_out'] or 0:>10,}"
                f"{row['reasoning'] or 0:>9,}"
                f"{int(row['avg_ms'] or 0):>7}ms"
                f"{_money(row['cost']):>13}"
                f"  {labels.get(row['stage'], row['stage'])}"
            )

        # ── الإجمالي ─────────────────────────────────────────────
        totals = events.aggregate(
            failed=Count("id", filter=Q(outcome=AiUsageEvent.Outcome.FAILED)),
            truncated=Count("id", filter=Q(outcome=AiUsageEvent.Outcome.TRUNCATED)),
            tokens_in=Sum("input_tokens"),
            cached=Sum("cached_input_tokens"),
            tokens_out=Sum("output_tokens"),
            cost=Sum("estimated_cost"),
            slowest=Max("duration_ms"),
        )
        self.stdout.write(self.style.MIGRATE_LABEL("\nالإجمالي"))
        self.stdout.write(f"  النداءات            {total:,}")
        self.stdout.write(f"  الفشل               {totals['failed']:,} ({_ratio(totals['failed'], total)})")
        self.stdout.write(f"  المبتور             {totals['truncated']:,} ({_ratio(totals['truncated'], total)})")
        self.stdout.write(
            f"  إصابة المخزَّن       {_ratio(totals['cached'] or 0, totals['tokens_in'] or 0)}"
            f"  ({totals['cached'] or 0:,} من {totals['tokens_in'] or 0:,} رمز إدخال)"
        )
        self.stdout.write(f"  رموز الإخراج        {totals['tokens_out'] or 0:,}")
        self.stdout.write(f"  أبطأ نداء           {int(totals['slowest'] or 0):,}ms")
        self.stdout.write(f"  الكلفة التقديرية    {_money(totals['cost'])}")

        # ── إعادة صياغة منصور ────────────────────────────────────
        # نداءٌ ثانٍ كامل لكل مسودة ضعيفة. نسبته إلى النداء الأول هي مؤشر جودة
        # البادئة: ارتفاعها يعني أن التعليمات لا المسودة هي المشكلة.
        first = events.filter(stage=AiUsageEvent.Stage.MANSOUR).count()
        rewrite = events.filter(stage=AiUsageEvent.Stage.MANSOUR_REWRITE).count()
        if first:
            self.stdout.write(self.style.MIGRATE_LABEL("\nإعادة صياغة منصور"))
            self.stdout.write(
                f"  {_count(rewrite, 'إعادة واحدة', 'إعادتان', 'إعادات', 'إعادة')}"
                f" من {_count(first, 'ردٍّ واحد', 'ردَّين', 'ردود', 'ردًّا')}"
                f" ({_ratio(rewrite, first)}) — كل إعادة نداءٌ ثانٍ كامل."
            )

        # ── أعلى المدارس ─────────────────────────────────────────
        limit = max(1, int(options["schools"]))
        school_rows = (
            events.exclude(school__isnull=True)
            .values("school__id", "school__name")
            .annotate(calls=Count("id"), cost=Sum("estimated_cost"), tokens=Sum("input_tokens"))
            .order_by("-calls")[:limit]
        )
        if school_rows:
            self.stdout.write(self.style.MIGRATE_LABEL(f"\nأعلى {limit} مدارس استهلاكًا"))
            for row in school_rows:
                self.stdout.write(
                    f"  {row['calls']:>6} نداء"
                    f"{_money(row['cost']):>13}"
                    f"   {(row['school__name'] or '—')[:40]}"
                )

        anonymous = events.filter(school__isnull=True).count()
        if anonymous:
            self.stdout.write(
                f"\n  و{_count(anonymous, 'نداءٌ واحد', 'نداءان', 'نداءات', 'نداءً')}"
                " بلا مدرسة (زوّار منصور) — كلفة المنصة لا كلفة عميل."
            )
        self.stdout.write("")
