# Releasing the backend

Production must always run a commit that is on `master`.

## Rules

1. **All work is built on `master`.** Start feature and fix branches from the current `origin/master`.
2. **Deploy only tags on `master`.** Merge the work into `master` first, then tag that `master` commit
   (`v2.5.47.<n>-<short-name>`, next free number; check `git tag -l "v2.5.47.*"`) and deploy that tag.
   Never deploy a tag whose commit is not reachable from `origin/master`
   (`git merge-base --is-ancestor <tag> origin/master` must succeed).
3. **Release branches are short-lived.** A `release/<n>-<name>` branch may only be created from
   `origin/master`, and it must be merged back into `master` (a real merge, no force push) immediately
   after its tag is deployed, before anything else is deployed. Do not branch a new release from an older
   release branch.
4. No force pushes or history rewriting on `master`.

## Steps

```bash
git fetch origin
git switch -c feat/<name> origin/master
# ... work, tests, makemigrations --check ...
# merge into master (fast-forward or merge commit), push origin master
git tag v2.5.47.<n>-<name> origin/master && git push origin v2.5.47.<n>-<name>
gh workflow run backend-deploy.yml -R ravisainiiitr/iic-booking-backend --ref master -f release_tag=v2.5.47.<n>-<name>
```

Deploy does not migrate. After every deploy run **Plan Production Migrations**; if it lists anything, run
`migrate-production.yml -f confirm_migrate=MIGRATE -f sync_email_templates=false`.

If two branches both add migrations to the same app, add a no-op merge migration
(`python manage.py makemigrations --merge`) on `master` before tagging.
