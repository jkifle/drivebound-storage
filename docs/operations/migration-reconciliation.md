# Migration reconciliation

## Duplicate `0014` incident

The storage lifecycle migration is the canonical `0014`. A short-lived,
unreleased node HTTP proxy migration accidentally reused that revision
identifier. The incompatible proxy was removed rather than placed in the
production graph. Fresh databases and databases that reached canonical `0014`
through `0017` can run the normal `alembic upgrade head` path.

Alembic records only the revision string, so it cannot distinguish a database
that ran the short-lived node migration as `0014` from one that ran canonical
storage `0014`. Before upgrading any database whose `alembic current` output is
exactly `0014`, take a verified backup and inspect both markers:

```sql
SELECT
  to_regclass('public.storage_policies') IS NOT NULL AS canonical_storage_0014,
  EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'paired_nodes'
      AND column_name = 'node_secret'
  ) AS short_lived_node_0014;
```

- `canonical_storage_0014=true` and `short_lived_node_0014=false` is the normal
  state; run `alembic upgrade head`.
- `canonical_storage_0014=false` and `short_lived_node_0014=true` is the
  ambiguous short-lived branch. Do not continue: restore the pre-migration
  backup, or have an operator remove that migration's unique constraint and
  column, stamp the verified schema back to `0013`, and then run the normal
  upgrade. Node credentials from that branch must be paired again.
- Any other combination means the schema was manually stamped or partially
  applied. Stop and reconcile it from a verified backup before changing the
  Alembic version table.

Never use `alembic stamp` as a substitute for confirming that the corresponding
schema objects and constraints exist.
