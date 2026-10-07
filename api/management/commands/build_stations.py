import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from stations import etl


class Command(BaseCommand):
    help = "Build stations.npz, places.json, usa.geojson, unmatched.csv and build_report.json."

    def add_arguments(self, parser):
        parser.add_argument("--download", action="store_true", help="fetch gazetteer sources")
        raw = settings.BASE_DIR / "data/raw/fuel-prices-for-be-assessment.csv"
        parser.add_argument("--csv", default=str(raw))
        parser.add_argument("--gazetteer", default=str(settings.BASE_DIR / "data/gazetteer"))
        parser.add_argument("--out", default=str(settings.BUILD_DIR))

    def handle(self, *args, **opts):
        gaz = Path(opts["gazetteer"])
        if opts["download"]:
            for key, status in etl.download(gaz).items():
                self.stdout.write(f"download {key}: {status}")
        try:
            report = etl.build(
                Path(opts["csv"]), gaz, Path(opts["out"]), settings.STATION_PRICE_RULE
            )
        except FileNotFoundError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(report, indent=2))
