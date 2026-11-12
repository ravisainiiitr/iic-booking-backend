# Local testing

## Backend (worktree `D:\IIC_NEW\wt-be-connect`)

- Create a `.env` with `DATABASE_URL` pointing at the **local** PostgreSQL, using a separate database name (for example `iic_connect`). Tests use `test_<name>`.
- Run the Connect suite:
  ```bash
  python -m pytest iic_booking/connect -q -p no:cacheprovider
  ```
  `--reuse-db` is the project default. It is about 200 tests and takes 2 to 4 minutes; run it in the background.
- Connect tests re-seed their own fixed data (General community, categories, badges, email templates). A reused test database whose migration data was flushed by a transactional test elsewhere still works.
- Before committing, check for missing migrations:
  ```bash
  python manage.py makemigrations --check --dry-run --settings=config.settings.test
  ```

## Frontend (worktree `D:\IIC_NEW\wt-fe-connect`)

- Junction `node_modules` from `D:\IIC_NEW\wt-fe-health\node_modules`. Remove the junction before `git worktree remove`.
- Connect tests:
  ```bash
  npx vitest run src/pages/connect
  ```
- Full suite:
  ```bash
  npx vitest run
  ```

## Local demo against a dev database

1. Create the local database (`iic_connect`) and run `python manage.py migrate`.
2. Seed demo data: `manage.py shell -c "exec(open(r'<scratchpad>\seed_connect_demo.py', encoding='utf-8').read())"`. It writes demo tokens to a local JSON file and refuses to run against a non-local database.
3. Start the backend with the module on:
   ```bash
   IITR_CONNECT_ENABLED=true python manage.py runserver 127.0.0.1:8000 --noreload
   ```
4. Start the frontend:
   ```bash
   npx vite --port 8080
   ```
   Vite proxies `/api` to port 8000.
5. Sign in by putting a demo token into `localStorage.auth_token` and loading `/api/auth/user/` into `localStorage.user`. Then open `http://localhost:8080/connect`.
6. Check access as different users:
   - another group's member opening a thread link (a neutral "not available" page);
   - signed out at `/equipment/<id>` (the public page, with no discussions).
