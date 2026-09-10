# Railway deployment

Deploy from `infra/docker/server.Dockerfile`. Configure the variables documented in `.env.example`
through Railway's secret store. The production release command will become
`python apps/server/manage.py migrate --noinput` after Session 2 adds the first application
migrations.

Use one web replica until migration execution and face-model memory behavior are proven. Add the
health check path `/health/`. Wildcard domain, secure cookie, trusted-origin, resource limit, and
rollback instructions belong to Session 9 and must be verified against the live environment.
