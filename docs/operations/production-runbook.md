# OpenFotos production runbook

This runbook is the source of truth for the isolated rehearsal and the clean first production
deployment. Do not paste a secret, certificate, database URL, API token, photograph, or model file
into this repository, GitHub Actions, an issue, or the evidence log.

## Fixed launch shape

- Railway web service and Supabase database: Singapore.
- Cloudflare R2: private bucket with the `APAC` location hint. A hint is best effort, not a
  country-residency guarantee.
- Public tenant: `https://balladsoflove.onenodeai.com`.
- Admin: `https://admin.onenodeai.com`, additionally protected by Cloudflare Access.
- Apex `https://onenodeai.com` remains on Cloudflare Pages.
- One Railway web replica and one Gunicorn worker initially. Increase only from load-test evidence.
- Supabase Micro initially. Upgrade to Small only when database metrics identify the database as the
  bottleneck.
- R2 is the delivery copy; Ballads of Love retains its edited local originals.

## Phase 1: create an isolated rehearsal

Create temporary resources with `rehearsal` in every provider-side name. Record their names, region,
creation time, and owner in `docs/operations/launch-evidence.md`; never record identifiers that grant
access.

### Supabase

1. Create a new project in Singapore on Micro compute. Generate the database password in a password
   manager and share it only with the two provider administrators.
2. In Database settings, enable SSL enforcement and download the CA certificate.
3. In Database > Extensions, confirm `vector` is available. In SQL Editor, run
   `infra/supabase/bootstrap.sql` as `postgres`.
4. From a trusted terminal, connect as `postgres` using the exact **Session pooler** string copied
   from the Connect dialog (port 5432). Run `\password openfotos_runtime` and generate a different
   password. The prompt prevents the password entering shell history or SQL Editor history.
5. Confirm API Settings exposes only the Supabase-managed schemas; do not add `openfotos` to the
   exposed schema list.
6. Build two session-pooler URLs. The migration URL uses `postgres.<project-ref>`; the runtime URL
   uses `openfotos_runtime.<project-ref>`. Percent-encode passwords and append:

   ```text
   sslmode=verify-full&sslrootcert=/tmp/openfotos-supabase-ca.pem&options=-csearch_path%3Dopenfotos%2Cextensions
   ```

   OpenFotos also explicitly sets and verifies `openfotos,extensions` after every deployed
   PostgreSQL connection because a pooler may discard URL startup options. A migration run is not
   accepted until `django_migrations` and `auth_user` exist in `openfotos` and are absent from
   `public`.

7. Base64-encode the CA certificate as one line locally. Store the result only in Railway as
   `DATABASE_CA_CERT_BASE64`. The migration and web services each receive the CA, but never each
   other's database URL.
8. Confirm Database > Backups shows daily backups. Rehearsal must include a restore into a separate
   project; restoring over the only copy is not a valid test.

### Cloudflare R2

1. Create a private rehearsal bucket with the APAC location hint. Do not enable public development
   URL access or a public custom domain.
2. Create an **account-owned** Object Read & Write token scoped only to that bucket. Record the
   access-key ID and secret once in Railway; never use an account-wide Admin token.
3. Upload `operations/readiness-sentinel.txt` containing only `openfotos readiness`. The readiness
   endpoint checks that this private object exists.
4. Set the R2 notification threshold low for rehearsal. Set production informational alerts below
   USD 50 so usage is visible before the accepted USD 50 monthly threshold.

### Railway

1. Create a rehearsal project from `openfotos/openfotos-platform`, using
   `infra/docker/server.Dockerfile`, and select Southeast Asia Metal (Singapore). Disable automatic
   deployment until the migration service has succeeded.
2. Create the separate, non-public migration service described in `infra/railway/README.md`. Give
   only that service `MIGRATION_DATABASE_URL`; never add it to the web service or a Railway
   pre-deploy environment. Run it manually and require a successful zero exit before each web
   deployment.
3. Create the web service with the deployed variables from `.env.example`. Generate independent
   random values of at least
   32 characters for `SHARE_PIN_PEPPER`, `PRIVACY_HASH_KEY`, and `CLOUDFLARE_ORIGIN_SECRET`, and at
   least 50 characters for `DJANGO_SECRET_KEY`. Set:

   ```text
   OPENFOTOS_ENVIRONMENT=rehearsal
   DJANGO_DEBUG=false
   PUBLIC_BASE_DOMAIN=onenodeai.com
   ADMIN_HOST=admin.onenodeai.com
   DJANGO_ALLOWED_HOSTS=.onenodeai.com,healthcheck.railway.app,<generated Railway hostname>
   DJANGO_CSRF_TRUSTED_ORIGINS=https://*.onenodeai.com
   STUDIO_PRIVACY_EMAIL=balladsoflove@gmail.com
   OPENFOTOS_RUNTIME_DB_ROLE=openfotos_runtime
   DJANGO_SECURE_HSTS_SECONDS=0
   GUNICORN_WORKERS=1
   GUNICORN_THREADS=4
   PORT=8000
   ```

4. Set the health path to `/health/live/`. Readiness is monitored separately at `/health/ready/`.
5. Set a Railway usage alert at USD 15 and a hard usage limit at USD 30. Treat either event as an
   incident; a hard limit can make the service unavailable.
6. After the migration service succeeds, deploy the web service. A failed migration is a manual
   release blocker. Confirm the running process has UID 10001 and that startup reports no
   model-contract or configuration error. Confirm the web service variable list contains
   `DATABASE_URL` and does not contain `MIGRATION_DATABASE_URL`.

### DNS, TLS, and edge controls

1. Add `*.onenodeai.com` as the Railway custom domain. Create every Railway-provided verification,
   wildcard CNAME, and ACME record exactly. Keep `_acme-challenge` DNS-only and proxy the wildcard
   application CNAME through Cloudflare. Do not alter the apex Pages records.
2. Enable Cloudflare Universal SSL and use Full origin encryption for this Railway proxied domain.
3. Create a request-header transform rule for the Railway application hosts that overwrites
   `X-OpenFotos-Origin` with the exact `CLOUDFLARE_ORIGIN_SECRET`. Test the rule with Cloudflare
   Trace. A visitor-supplied value must be overwritten.
4. Create a Cloudflare Access self-hosted application for `admin.onenodeai.com`. The Allow policy
   contains only the primary operator and trusted backup person's separate identities. Do not use
   `Everyone` or “all valid emails.” Use a short Access session and test in a private browser.
5. Confirm all four properties:
   - tenant and legal pages work through Cloudflare;
   - admin redirects to Cloudflare Access before Django login;
   - the raw Railway hostname returns 404 for application and admin routes;
   - an unknown tenant host returns 404.

## Phase 2: seed and rehearse

1. Create the private Django superuser interactively through Railway SSH. Railway's SSH maintenance
   session is root even though deployed PID 1 must have UID 10001. Verify `/proc/1/status`, repair
   `/tmp/openfotos-supabase-ca.pem` to owner/group 10001 and mode 0600 if a prior root diagnostic
   replaced it, then enter `openfotos` with a preserved service environment and
   `HOME=/home/openfotos`. Never pass the password on the command line, and never run migrations
   from the runtime service.
2. Through the dedicated admin host, create the photographer user, `balladsoflove` Photographer,
   and active membership. The photographer is nonstaff. Do not create the local testing account in
   production.
3. Install the draft desktop release on one Windows x64 and one Apple Silicon machine. Verify the
   published `SHA256SUMS`; record the hashes and smoke-test results.
4. Upload only the consented rehearsal set. The three reference files in
   `private_benchmark_media/myphotos` remain local and ignored by Git.
5. Exercise: event create, multi-batch upload, pause/resume, retry, publish, PIN unlock, gallery,
   reference search, download, capability revocation, password reset, and workstation revocation.
6. Run gallery and unlock load checks from a trusted machine:

   ```text
   OPENFOTOS_LOAD_PORTAL_URL='<private HTTPS portal URL>' OPENFOTOS_LOAD_PORTAL_PIN='<PIN>' uv run --extra desktop python scripts/load_test.py gallery --users 100
   OPENFOTOS_LOAD_PORTAL_URL='<private HTTPS portal URL>' OPENFOTOS_LOAD_PORTAL_PIN='<PIN>' uv run --extra desktop python scripts/load_test.py unlock --users 100
   ```

   Never paste the command output with its shell history into a public artifact. The script itself
   reports no URL or PIN. Acceptance is under 1% errors and p95 at most 2 seconds.
7. Search throttling permits ten attempts per source address per 15 minutes. Run search tests from
   controlled distributed egress addresses, no more than ten users per address, until 100 total
   concurrent users are represented. Each runner uses an explicitly consented reference:

   ```text
   OPENFOTOS_LOAD_PORTAL_URL='<private HTTPS portal URL>' OPENFOTOS_LOAD_PORTAL_PIN='<PIN>' uv run --extra desktop python scripts/load_test.py search --users 10 --reference-photo private_benchmark_media/myphotos/<file> --confirm-reference-consent
   ```

   The script clears every successful result set. Acceptance is under 1% errors and p95 at most 10
   seconds. If distributed execution is unavailable, record that the 100-user search SLO is
   unproven; do not describe a ten-user result as equivalent.
8. Schedule a Railway cron service every 15 minutes for
   `python apps/server/manage.py purge_expired_face_searches`. Schedule a daily cron service for
   `python apps/server/manage.py report_event_retention --days-ahead 7`. Both use the same TLS
   wrapper and runtime URL. Never schedule `purge_expired_event_media`; event purge requires studio
   confirmation and `--confirm`.
9. Rehearse an R2 outage, leaked-link revocation, photographer password reset, pepper rotation,
   whole-event privacy erasure, due retention purge, and the incident contacts below.
10. Restore the daily database backup into a separate Supabase project, point a temporary Railway
    service at an empty rehearsal bucket, and verify schema, row counts, login, and expected missing
    delivery objects. Record the measured recovery time and data cutoff. The target is RPO about 24
    hours and restoration within 24 hours.

## Phase 3: destroy rehearsal and create production

Export only the non-secret evidence log. Delete the rehearsal Railway project, R2 token and bucket,
and Supabase project after confirming the studio retains its local edited originals. Provider backup
copies tied to a deleted Supabase project are not recoverable.

Create clean production resources by repeating Phase 1; do not clone the rehearsal database,
bucket, roles, tokens, or secrets. Set `OPENFOTOS_ENVIRONMENT=production`; keep HSTS at 300 seconds
for the first live day. Raise it only after both hosts and rollback have been verified. Seed only the
production superuser, photographer account, tenant, and membership.

## Routine operations

- Review Railway, Supabase, and R2 usage and alerts daily during the pilot.
- Review the retention report daily. Obtain written studio confirmation naming the exact event ID,
  then run `purge_expired_event_media --event-id <UUID> --confirm` from the migration container.
- Run `reset_photographer_password <username> --confirm` interactively for account recovery; it
  revokes browser and desktop sessions.
- For a verified removal request, immediately run `erase_event_for_privacy` with the exact event
  ID/name and a non-PII instruction reference. Retry after any active five-minute object lease and
  finish within 24 hours. Rebuild a replacement as a new event without the disputed photograph.
- Check Supabase daily-backup status. Perform a separate-project restore rehearsal at least before
  every major release.
- Run `scripts/termux_monitor.sh` every five minutes using Termux:API plus a scheduler. Android
  battery optimization must be disabled for Termux; this monitor is supplementary and may be killed
  by the OS.

## Incident response

Contain first: unpublish the affected event, revoke capabilities or credentials, rotate the exposed
secret, and block traffic if necessary. Do not wait for root-cause certainty. Notify Ballads of Love
within four hours of a suspected privacy or access incident, using a private channel. Include what is
known, containment, affected event IDs (not names or photos), next update time, and required studio
action. Preserve privacy-safe Railway/Django audit evidence; never export request bodies, private
URLs, PINs, reference images, face crops, or embeddings.

The active launch support window is one hour before through four hours after launch. Urgent support
continues for 24 hours. The primary operator owns technical containment; the trusted backup person
can revoke provider access and restore service; Ballads of Love owns participant communication and
verified removal instructions.

## Rollback

For an application regression, redeploy the last known-good Railway image. Never reverse a migration
unless its reviewed reverse operation is known safe. If the new schema is incompatible, stop writes,
restore the pre-deploy database backup to a separate project, validate it, then repoint the service.
R2 objects are never overwritten during rollback without comparing their exact keys and hashes.
