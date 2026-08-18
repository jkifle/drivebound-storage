import * as SQLite from "expo-sqlite";

export type TransferState = "queued" | "uploading" | "retry" | "complete" | "failed";
export type Transfer = {
  localVersion: string;
  connectionScope: string;
  localId: string;
  filename: string;
  mediaType: string;
  mimeType: string;
  fileSize: number;
  createdAt: string;
  modifiedAt: string;
  state: TransferState;
  uploadId: string | null;
  uploadUrl: string | null;
  offset: number;
  chunkSize: number | null;
  attempts: number;
  retryAt: number;
  lastError: string | null;
  leaseOwner: string | null;
  leaseExpiresAt: number;
  updatedAt: number;
};

export type NewTransfer = Omit<
  Transfer,
  | "state"
  | "uploadId"
  | "uploadUrl"
  | "offset"
  | "chunkSize"
  | "attempts"
  | "retryAt"
  | "lastError"
  | "leaseOwner"
  | "leaseExpiresAt"
  | "updatedAt"
>;

export type QueueSummary = {
  pending: number;
  uploading: number;
  failed: number;
  complete: number;
  bytesPending: number;
  nextRetryAt: number | null;
};

const TRANSFER_LEASE_MS = 5 * 60 * 1000;
const RUN_LEASE_MS = 5 * 60 * 1000;
let databasePromise: Promise<SQLite.SQLiteDatabase> | null = null;

async function addMissingColumn(db: SQLite.SQLiteDatabase, name: string, definition: string): Promise<void> {
  const columns = await db.getAllAsync<{ name: string }>("PRAGMA table_info(transfers)");
  if (!columns.some(column => column.name === name)) {
    await db.execAsync(`ALTER TABLE transfers ADD COLUMN ${name} ${definition}`);
  }
}

async function database() {
  if (!databasePromise) {
    databasePromise = (async () => {
      const db = await SQLite.openDatabaseAsync("drivebound-transfer-queue.db");
      await db.execAsync(`
        PRAGMA journal_mode = WAL;
        PRAGMA busy_timeout = 5000;
        CREATE TABLE IF NOT EXISTS transfers (
          local_version TEXT PRIMARY KEY,
          connection_scope TEXT NOT NULL DEFAULT '',
          local_id TEXT NOT NULL,
          filename TEXT NOT NULL,
          media_type TEXT NOT NULL,
          mime_type TEXT NOT NULL,
          file_size INTEGER NOT NULL,
          created_at TEXT NOT NULL,
          modified_at TEXT NOT NULL,
          state TEXT NOT NULL,
          upload_id TEXT,
          upload_url TEXT,
          offset INTEGER NOT NULL DEFAULT 0,
          chunk_size INTEGER,
          attempts INTEGER NOT NULL DEFAULT 0,
          retry_at INTEGER NOT NULL DEFAULT 0,
          last_error TEXT,
          lease_owner TEXT,
          lease_expires_at INTEGER NOT NULL DEFAULT 0,
          updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS backup_run_locks (
          connection_scope TEXT PRIMARY KEY,
          owner TEXT NOT NULL,
          expires_at INTEGER NOT NULL
        );
      `);

      // Upgrade databases created by earlier mobile builds without discarding
      // unfinished transfers. Unscoped legacy rows cannot safely be associated
      // with a newly connected account, so they are intentionally removed.
      await addMissingColumn(db, "connection_scope", "TEXT NOT NULL DEFAULT ''");
      await addMissingColumn(db, "lease_owner", "TEXT");
      await addMissingColumn(db, "lease_expires_at", "INTEGER NOT NULL DEFAULT 0");
      await db.execAsync(`
        CREATE INDEX IF NOT EXISTS transfers_pending_v2
          ON transfers(connection_scope, state, retry_at, lease_expires_at, updated_at);
        DELETE FROM transfers WHERE connection_scope = '';
        UPDATE transfers
          SET state = 'retry', retry_at = 0, lease_owner = NULL, lease_expires_at = 0
          WHERE state = 'uploading' AND lease_expires_at <= 0;
      `);
      return db;
    })();
  }
  return databasePromise;
}

function fromRow(row: Record<string, unknown>): Transfer {
  return {
    localVersion: String(row.local_version),
    connectionScope: String(row.connection_scope),
    localId: String(row.local_id),
    filename: String(row.filename),
    mediaType: String(row.media_type),
    mimeType: String(row.mime_type),
    fileSize: Number(row.file_size),
    createdAt: String(row.created_at),
    modifiedAt: String(row.modified_at),
    state: row.state as TransferState,
    uploadId: row.upload_id ? String(row.upload_id) : null,
    uploadUrl: row.upload_url ? String(row.upload_url) : null,
    offset: Number(row.offset),
    chunkSize: row.chunk_size === null || row.chunk_size === undefined ? null : Number(row.chunk_size),
    attempts: Number(row.attempts),
    retryAt: Number(row.retry_at),
    lastError: row.last_error ? String(row.last_error) : null,
    leaseOwner: row.lease_owner ? String(row.lease_owner) : null,
    leaseExpiresAt: Number(row.lease_expires_at),
    updatedAt: Number(row.updated_at),
  };
}

export async function enqueueTransfer(input: NewTransfer): Promise<"queued" | "pending" | "complete"> {
  const db = await database();
  const now = Date.now();
  let outcome: "queued" | "pending" | "complete" = "queued";
  await db.withExclusiveTransactionAsync(async transaction => {
    const existing = await transaction.getFirstAsync<{ state: TransferState }>(
      "SELECT state FROM transfers WHERE local_version = ?",
      input.localVersion,
    );
    if (existing?.state === "complete") outcome = "complete";
    else if (existing) outcome = "pending";
    await transaction.runAsync(
      `INSERT INTO transfers (
         local_version, connection_scope, local_id, filename, media_type, mime_type,
         file_size, created_at, modified_at, state, offset, attempts, retry_at,
         lease_expires_at, updated_at
       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, 0, 0, 0, ?)
       ON CONFLICT(local_version) DO UPDATE SET
         connection_scope = excluded.connection_scope,
         local_id = excluded.local_id,
         filename = excluded.filename,
         media_type = excluded.media_type,
         mime_type = excluded.mime_type,
         file_size = CASE WHEN excluded.file_size > 0 THEN excluded.file_size ELSE transfers.file_size END,
         created_at = excluded.created_at,
         modified_at = excluded.modified_at,
         updated_at = excluded.updated_at
       WHERE transfers.state != 'complete'`,
      input.localVersion,
      input.connectionScope,
      input.localId,
      input.filename,
      input.mediaType,
      input.mimeType,
      input.fileSize,
      input.createdAt,
      input.modifiedAt,
      now,
    );
    // A modified photo gets a new version key. Its older completed queue record
    // is no longer needed, but the current record must remain as durable proof
    // that this exact local version was backed up.
    await transaction.runAsync(
      `DELETE FROM transfers
       WHERE connection_scope = ? AND local_id = ? AND local_version != ? AND state = 'complete'`,
      input.connectionScope,
      input.localId,
      input.localVersion,
    );
  });
  return outcome;
}

export async function acquireBackupRun(connectionScope: string, owner: string): Promise<boolean> {
  const db = await database();
  const now = Date.now();
  let acquired = false;
  await db.withExclusiveTransactionAsync(async transaction => {
    await transaction.runAsync("DELETE FROM backup_run_locks WHERE expires_at <= ?", now);
    const existing = await transaction.getFirstAsync<{ owner: string }>(
      "SELECT owner FROM backup_run_locks WHERE connection_scope = ?",
      connectionScope,
    );
    if (!existing || existing.owner === owner) {
      await transaction.runAsync(
        `INSERT INTO backup_run_locks(connection_scope, owner, expires_at) VALUES (?, ?, ?)
         ON CONFLICT(connection_scope) DO UPDATE SET owner = excluded.owner, expires_at = excluded.expires_at`,
        connectionScope,
        owner,
        now + RUN_LEASE_MS,
      );
      acquired = true;
    }
  });
  return acquired;
}

export async function renewBackupRun(connectionScope: string, owner: string): Promise<boolean> {
  const db = await database();
  const result = await db.runAsync(
    "UPDATE backup_run_locks SET expires_at = ? WHERE connection_scope = ? AND owner = ?",
    Date.now() + RUN_LEASE_MS,
    connectionScope,
    owner,
  );
  return result.changes === 1;
}

export async function releaseBackupRun(connectionScope: string, owner: string): Promise<void> {
  const db = await database();
  await db.runAsync("DELETE FROM backup_run_locks WHERE connection_scope = ? AND owner = ?", connectionScope, owner);
  await db.runAsync(
    `UPDATE transfers
     SET state = 'retry', lease_owner = NULL, lease_expires_at = 0, updated_at = ?
     WHERE connection_scope = ? AND state = 'uploading' AND lease_owner = ?`,
    Date.now(),
    connectionScope,
    owner,
  );
}

export async function claimNextTransfer(connectionScope: string, owner: string): Promise<Transfer | null> {
  const db = await database();
  const now = Date.now();
  let claimed: Transfer | null = null;
  await db.withExclusiveTransactionAsync(async transaction => {
    await transaction.runAsync(
      `UPDATE transfers
       SET state = 'retry', lease_owner = NULL, lease_expires_at = 0, updated_at = ?
       WHERE connection_scope = ? AND state = 'uploading' AND lease_expires_at <= ?`,
      now,
      connectionScope,
      now,
    );
    const row = await transaction.getFirstAsync<Record<string, unknown>>(
      `SELECT * FROM transfers
       WHERE connection_scope = ? AND state IN ('queued', 'retry') AND retry_at <= ?
       ORDER BY CASE state WHEN 'retry' THEN 0 ELSE 1 END, updated_at
       LIMIT 1`,
      connectionScope,
      now,
    );
    if (!row) return;
    const localVersion = String(row.local_version);
    const result = await transaction.runAsync(
      `UPDATE transfers
       SET state = 'uploading', lease_owner = ?, lease_expires_at = ?, updated_at = ?
       WHERE local_version = ? AND connection_scope = ? AND state IN ('queued', 'retry')`,
      owner,
      now + TRANSFER_LEASE_MS,
      now,
      localVersion,
      connectionScope,
    );
    if (result.changes === 1) {
      const next = await transaction.getFirstAsync<Record<string, unknown>>(
        "SELECT * FROM transfers WHERE local_version = ?",
        localVersion,
      );
      if (next) claimed = fromRow(next);
    }
  });
  return claimed;
}

type MutableTransferFields = Pick<
  Transfer,
  | "state"
  | "uploadId"
  | "uploadUrl"
  | "offset"
  | "chunkSize"
  | "attempts"
  | "retryAt"
  | "lastError"
  | "fileSize"
  | "leaseOwner"
  | "leaseExpiresAt"
>;

export async function updateTransfer(
  localVersion: string,
  changes: Partial<MutableTransferFields>,
  owner?: string,
): Promise<boolean> {
  const entries = Object.entries(changes);
  if (!entries.length) return true;
  const db = await database();
  const fields = entries
    .map(([field]) => `${field.replace(/[A-Z]/g, character => `_${character.toLowerCase()}`)} = ?`)
    .join(", ");
  const ownerClause = owner ? " AND lease_owner = ?" : "";
  const result = await db.runAsync(
    `UPDATE transfers SET ${fields}, updated_at = ? WHERE local_version = ?${ownerClause}`,
    ...entries.map(([, value]) => value),
    Date.now(),
    localVersion,
    ...(owner ? [owner] : []),
  );
  return result.changes === 1;
}

export async function renewTransferLease(localVersion: string, owner: string): Promise<boolean> {
  return updateTransfer(localVersion, { leaseExpiresAt: Date.now() + TRANSFER_LEASE_MS }, owner);
}

export async function queueSummary(connectionScope: string): Promise<QueueSummary> {
  const db = await database();
  const rows = await db.getAllAsync<{ state: TransferState; count: number; bytes: number }>(
    `SELECT state, COUNT(*) AS count,
      COALESCE(SUM(CASE WHEN file_size > offset THEN file_size - offset ELSE 0 END), 0) AS bytes
     FROM transfers WHERE connection_scope = ? GROUP BY state`,
    connectionScope,
  );
  const values: QueueSummary = { pending: 0, uploading: 0, failed: 0, complete: 0, bytesPending: 0, nextRetryAt: null };
  for (const row of rows) {
    const count = Number(row.count);
    if (row.state === "complete") values.complete = count;
    else if (row.state === "failed") values.failed = count;
    else {
      values.pending += count;
      values.bytesPending += Number(row.bytes);
      if (row.state === "uploading") values.uploading = count;
    }
  }
  const retry = await db.getFirstAsync<{ retry_at: number | null }>(
    `SELECT MIN(retry_at) AS retry_at FROM transfers
     WHERE connection_scope = ? AND state = 'retry' AND retry_at > ?`,
    connectionScope,
    Date.now(),
  );
  values.nextRetryAt = retry?.retry_at ? Number(retry.retry_at) : null;
  return values;
}

export async function listTransfers(connectionScope: string, limit = 12): Promise<Transfer[]> {
  const db = await database();
  const rows = await db.getAllAsync<Record<string, unknown>>(
    `SELECT * FROM transfers
     WHERE connection_scope = ? AND state != 'complete'
     ORDER BY CASE state WHEN 'uploading' THEN 0 WHEN 'failed' THEN 1 WHEN 'retry' THEN 2 ELSE 3 END, updated_at DESC
     LIMIT ?`,
    connectionScope,
    limit,
  );
  return rows.map(fromRow);
}

export async function retryFailedTransfers(connectionScope: string): Promise<void> {
  const db = await database();
  await db.runAsync(
    `UPDATE transfers
     SET state = 'retry', attempts = 0, retry_at = 0, last_error = NULL,
       lease_owner = NULL, lease_expires_at = 0, updated_at = ?
     WHERE connection_scope = ? AND state = 'failed'`,
    Date.now(),
    connectionScope,
  );
}

export async function cancelPendingTransfers(connectionScope: string): Promise<number> {
  const db = await database();
  const result = await db.runAsync(
    `DELETE FROM transfers
     WHERE connection_scope = ? AND state IN ('queued', 'retry', 'failed')`,
    connectionScope,
  );
  return result.changes;
}
