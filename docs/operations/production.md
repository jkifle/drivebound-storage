# Production and staging operation baseline

## Secrets

Use a secret manager that can mount values as files (Docker secrets, Kubernetes
Secrets with envelope encryption, or the hosting provider's secret volume). Use
the production overlay with `docker compose -f docker-compose.yml -f
docker-compose.production.yml`.

Required file-backed values are `database_url`, `redis_url`, `jwt_secret`,
`mfa_encryption_secret`, and `media_encryption_master_key`. The API refuses to
start in staging or production when these are absent, when HTTPS is not
configured, or when the development encryption key is used.

When using the bundled PostgreSQL service, mount `postgres_password` as well
and use that same value in the file-backed `database_url`. In a managed
database deployment, omit the local PostgreSQL service and provide only its
credentialed connection URL through the secret manager.

Keep the public API behind a TLS reverse proxy or tunnel. The compose ports are
loopback-only by design. Configure HTTPS `APP_URL`, `API_URL`, and
`CONTROL_PLANE_URL`, then enable secure cookies and explicit CORS hosts.

## Environments and delivery

- **Development:** local Docker, console email, public development encryption
  key only for disposable data.
- **Staging:** production-shaped secrets, an isolated database/storage bucket,
  release-candidate images, and synthetic media only.
- **Production:** separate secrets, encrypted persistent volumes, versioned
  releases, and an approval gate after staging smoke tests.

The CI workflow validates backend tests, mobile type checking, frontend build,
Docker configuration, and an Alembic upgrade from an empty PostgreSQL volume.

## Observability and recovery

The API emits correlation-aware request logs and exposes Prometheus text metrics
at `/metrics`. Keep that endpoint private to the metrics scraper. Forward JSON
logs to the chosen low-cost managed collector and preserve `X-Request-ID` and
`traceparent` across the proxy and control plane.

At least monthly, restore a recent PostgreSQL backup plus a representative media
replica into the staging environment. Verify account login, timeline access,
preview decryption, checksum recovery, and a rollback to the prior application
image. Record the elapsed recovery time and gaps in the operations log.
