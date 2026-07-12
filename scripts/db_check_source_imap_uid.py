import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")

import django  # noqa: E402

django.setup()

from django.db import connection  # noqa: E402


def main() -> None:
    with connection.cursor() as c:
        c.execute(
            """
            select 1
            from information_schema.columns
            where table_schema = 'public'
              and table_name = 'users_walletrechargeparseentry'
              and column_name = 'source_imap_uid'
            """
        )
        print("col_exists", c.fetchone() is not None)
        c.execute(
            """
            select applied
            from django_migrations
            where app = 'users'
              and name = '0067_walletrechargeparseentry_source_imap_uid'
            """
        )
        print("migration_row", c.fetchone())


if __name__ == "__main__":
    main()

