# Railway deployment

Deploy from `infra/docker/server.Dockerfile`. Configure the variables documented in `.env.example`
through Railway's secret store. Use `python apps/server/manage.py migrate --noinput` as the
production release command. The container build collects versioned static assets for WhiteNoise.

Use one web replica until migration execution and face-model memory behavior are proven. Add the
health check path `/health/`. Wildcard domain, secure cookie, trusted-origin, resource limit, and
rollback instructions belong to Session 9 and must be verified against the live environment.
