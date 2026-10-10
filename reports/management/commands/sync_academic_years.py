from django.core.management.base import BaseCommand

from reports.academic_years import sync_academic_years


class Command(BaseCommand):
    help = "Synchronize current school and independent teacher year from the published Ministry calendar."

    def handle(self, *args, **options):
        result = sync_academic_years()
        if not result["value"]:
            self.stdout.write("No published calendar has started; no changes made.")
            return
        self.stdout.write(self.style.SUCCESS(
            f"Academic year {result['value']}: {result['schools']} schools and {result['teachers']} personal workspaces synchronized."
        ))
