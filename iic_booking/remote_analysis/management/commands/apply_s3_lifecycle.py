"""Plan (default) or apply the RAA large-data S3 lifecycle rules. Never deletes research data.

    python manage.py apply_s3_lifecycle                       # dry run: prints the merged configuration
    python manage.py apply_s3_lifecycle --apply               # writes it (keeps every other rule)
    python manage.py apply_s3_lifecycle --archive-after-days 180 --archive-class GLACIER_IR
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Plan or apply the Remote Analysis large-data S3 lifecycle rules (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write the configuration (default: dry run).")
        parser.add_argument("--archive-after-days", type=int, default=None, help="Optional; empty = no archive transition.")
        parser.add_argument("--archive-class", default="GLACIER_IR")

    def handle(self, *args, **opts):
        from iic_booking.my_research import storage as research_storage
        from iic_booking.remote_analysis.large_data import lifecycle

        try:
            desired = lifecycle.desired_rules(
                archive_after_days=opts["archive_after_days"], archive_class=opts["archive_class"]
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        lifecycle.check_rules(desired)
        buckets = lifecycle.buckets()
        if not buckets:
            raise CommandError("No bucket configured (AWS_STORAGE_BUCKET_NAME / MY_RESEARCH_S3_BUCKET).")
        client = research_storage._client()
        mode = "APPLY" if opts["apply"] else "DRY RUN"
        for index, bucket in enumerate(buckets, start=1):
            label = f"bucket{index}"  # bucket names are not printed (public Action logs)
            existing = lifecycle.read_existing(client, bucket)
            merged = lifecycle.merge(existing, desired)
            lifecycle.check_rules(merged)
            self.stdout.write(f"RESULT {mode} {label}: existing_rules={len(existing)} merged_rules={len(merged)}")
            for note in lifecycle.overlaps(existing):
                self.stdout.write(f"RESULT note {label}: {note}")
            for rule in desired:
                self.stdout.write(f"RESULT rule {label}: {json.dumps(rule, sort_keys=True)}")
            if not opts["apply"]:
                continue
            client.put_bucket_lifecycle_configuration(Bucket=bucket, LifecycleConfiguration={"Rules": merged})
            after = lifecycle.read_existing(client, bucket)
            ours = sorted(r["ID"] for r in after if str(r.get("ID", "")).startswith(lifecycle.RULE_PREFIX))
            kept = len([r for r in after if not str(r.get("ID", "")).startswith(lifecycle.RULE_PREFIX)])
            ok = ours == sorted(r["ID"] for r in desired) and kept == len(merged) - len(desired)
            self.stdout.write(f"RESULT verify {label}: {'PASS' if ok else 'FAIL'} ours={ours} other_rules_kept={kept}")
            if not ok:
                raise CommandError("Verification failed")
