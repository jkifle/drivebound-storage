# Production and staging operation baseline

## Secrets

Use a secret manager that can mount values as files (Docker secrets, Kubernetes
Secrets with envelope encryption, or the hosting provider's secret volume). Use
the production overlay with `docker compose -f docker-compose.yml -f
docker-compose.production.yml`.

Required file-backed values are `database_url`, `redis_url`, `jwt_secret`,
`mfa_encryption_secret`, `media_encryption_master_key`, and
`metrics_auth_token`. The API refuses to
start in staging or production when these are absent, when HTTPS is not
configured, or when the development encryption key is used.

When using the bundled PostgreSQL service, mount `postgres_password` as well
and use that same value in the file-backed `database_url`. In a managed
database deployment, omit the local PostgreSQL service and provide only its
credentialed connection URL through the secret manager.

Keep the public API behind a TLS reverse proxy or tunnel. The compose ports are
loopback-only by design. Configure HTTPS `APP_URL`, `API_URL`, and
`CONTROL_PLANE_URL`, then enable secure cookies and explicit CORS hosts. Set
`TRUSTED_PROXY_CIDRS` only to the direct proxy network(s) that are allowed to
supply `X-Forwarded-For`; Drivebound ignores forwarded client addresses from
every other peer so audit history and login throttling cannot be spoofed. The
trusted proxy must replace, not blindly append to, any client-supplied
`X-Forwarded-For` header.

## Environments and delivery

- **Development:** local Docker, console email, public development encryption
  key only for disposable data.
- **Staging:** production-shaped secrets, an isolated database/storage bucket,
  release-candidate images, and synthetic media only.
- **Production:** separate secrets, encrypted persistent volumes, versioned
  releases, and an approval gate after staging smoke tests.

The CI workflow validates backend tests, mobile type checking, frontend build,
Docker configuration, and an Alembic upgrade from an empty PostgreSQL volume.

## Account-deletion rollout and rollback

Migration `0017` introduces durable deletion jobs and makes inviter attribution
nullable. Before exposing account deletion, block the deletion endpoint at the
ingress, apply the migration, deploy the new application image, and drain every
pre-`0017` Celery worker. Start only workers and the scheduler from the new
image, verify the independent suppression-ledger mount is durable, then reopen
the endpoint. Older workers do not have the account/path fences required by the
deletion workflow.

Schema rollback is intentionally not automatic. Stop new deletion requests,
pause the scheduler, drain current workers and queues, and reconcile every
pending/retry/running deletion before stopping the application. Archive the
suppression ledger and deletion evidence outside the rollback target. The
`0017` downgrade refuses to run while any deletion-job row remains or while a
preserved album membership has null inviter attribution; both conditions need
an explicit, reviewed data migration. Only after evidence is archived and the
database is reconciled may operators remove those rows, stop all new-version
processes, downgrade to `0016`, and start the old application and workers.

Follow the complete [account-deletion operations runbook](account-deletion.md).

## Observability and recovery

The API emits correlation-aware request logs and exposes Prometheus text metrics
at `/metrics`. Remote deployments require the scraper to send
`Authorization: Bearer <metrics_auth_token>`; the endpoint must also remain
network-private. Metrics and logs use route templates instead of raw identifiers
to avoid leaking resource IDs or creating unbounded label cardinality. Forward JSON
logs to the chosen low-cost managed collector and preserve `X-Request-ID` and
`traceparent` across the proxy and control plane.

At least monthly, restore a recent PostgreSQL backup plus a representative media
replica into the staging environment. Verify account login, timeline access,
preview decryption, checksum recovery, and a rollback to the prior application
image. Record the elapsed recovery time and gaps in the operations log. Follow
the step-by-step [recovery drill runbook](recovery-drill.md); never test a
database restore against the live database.
