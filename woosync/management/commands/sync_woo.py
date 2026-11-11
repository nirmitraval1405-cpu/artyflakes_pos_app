from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from woosync.client import WooClient, WooError
from woosync.sync import SyncBusy, SyncOff, match_products, run_sync


class Command(BaseCommand):
    help = "Brings in new website orders, then sends stock to the website."

    def add_arguments(self, parser):
        parser.add_argument("--match", action="store_true", help="Match website products to POS products first.")

    def handle(self, *args, **options):
        stamp = timezone.localtime().strftime("%Y-%m-%d %H:%M:%S")
        try:
            if options["match"]:
                result = match_products(WooClient())
                self.stdout.write(f"{stamp} matched {result['matched']} products")
            run = run_sync("scheduled")
        except (SyncOff, SyncBusy) as exc:
            self.stdout.write(f"{stamp} skipped: {exc}")
            return
        except WooError as exc:
            raise CommandError(f"{stamp} {exc}")

        self.stdout.write(f"{stamp} {'OK' if run.ok else 'FAILED'}: {run.summary}")
        if not run.ok:
            raise CommandError("The sync reported a problem.")