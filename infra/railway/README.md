# Railway deployment

Deploy from `infra/docker/server.Dockerfile`. Configure the variables documented in `.env.example`
through Railway's secret store. Use `python apps/server/manage.py migrate --noinput` as the
production release command. The container build collects versioned static assets for WhiteNoise.

Use one web replica until migration execution and face-model memory behavior are proven. Add the
health check path `/health/`. Wildcard domain, secure cookie, trusted-origin, resource limit, and
rollback instructions belong to Session 9 and must be verified against the live environment.

Face search also requires the accepted hash-verified YuNet and SFace artifacts at the private paths
configured by `FACE_DETECTOR_MODEL_PATH` and `FACE_RECOGNIZER_MODEL_PATH`. Session 9 must choose and
rehearse their persistent deployment mechanism; model weights do not belong in the image source or
repository. Schedule `python apps/server/manage.py purge_expired_face_searches` at least every 15
minutes. Do not deploy until the Session 9 Railway/Supabase/R2 region, secret, backup, retention,
and cleanup decisions are complete.
