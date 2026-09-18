# Railway deployment

Deploy both services from `infra/docker/server.Dockerfile`. The web service receives the variables
documented in `.env.example` through Railway's secret store. Download the Supabase CA certificate
and store only its base64 encoding in `DATABASE_CA_CERT_BASE64`; never commit the certificate.

Do not put `MIGRATION_DATABASE_URL` in the web service or its pre-deploy environment. Railway makes
pre-deploy variables available to the deployed service too, which would let a compromised web
process recover migration privileges.

Create a separate, non-public migration service from the same image. Disable automatic deployment,
set its restart policy to Never, and give it only `OPENFOTOS_ENVIRONMENT`, `DJANGO_DEBUG=false`,
`DJANGO_SECRET_KEY`, `SHARE_PIN_PEPPER`, `DATABASE_CA_CERT_BASE64`,
`OPENFOTOS_RUNTIME_DB_ROLE`, and `MIGRATION_DATABASE_URL`. Override its start command with:

```text
python -m openfotos_server.database_tls --database-url-variable MIGRATION_DATABASE_URL -- sh -c 'python apps/server/manage.py migrate --noinput && python apps/server/manage.py configure_runtime_database_privileges'
```

Run that service manually and require a successful zero exit before deploying the web service. It
must have no domain, runtime database URL, R2 credentials, or Cloudflare origin secret. The web and
cron services receive only the restricted-role `DATABASE_URL` and never the migration URL.

Both URLs must set `sslmode=verify-full` and
`sslrootcert=/tmp/openfotos-supabase-ca.pem`, plus the URL-encoded option
`options=-csearch_path%3Dopenfotos%2Cextensions`. The second command reapplies runtime grants after
each migration. The container build collects versioned static assets for WhiteNoise.

Use one web replica until migration execution and face-model memory behavior are proven. Add the
health check path `/health/live/`; monitor dependency readiness separately at `/health/ready/`.

The image downloads the exact accepted YuNet and SFace artifacts during build, verifies their
frozen SHA-256 digests, and verifies them again before Gunicorn starts. Model weights remain absent
from source control. Schedule `python apps/server/manage.py purge_expired_face_searches` at least
every 15 minutes and `python apps/server/manage.py report_event_retention --days-ahead 7` daily.
Never schedule irreversible event purge. Follow `docs/operations/production-runbook.md` before
creating or changing provider resources.
