from django.core.management.base import BaseCommand

from iic_booking.research_copilot.services.seed_knowledge import seed_baseline_knowledge


class Command(BaseCommand):
    help = "Seed baseline IIC Research Copilot knowledge articles and index them (AI.2)."

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true", help="Re-index existing seed titles")

    def handle(self, *args, **options):
        result = seed_baseline_knowledge(force=bool(options.get("force")))
        self.stdout.write(self.style.SUCCESS(f"Knowledge seed complete: {result}"))
