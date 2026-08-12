import * as SQLite from "expo-sqlite";

export type TransferState = "queued" | "uploading" | "retry" | "complete" | "failed";
export type Transfer = {
  localVersion: string; localId: string; filename: string; mediaType: string; mimeType: string;
  fileSize: number; createdAt: string; modifiedAt: string; state: TransferState; uploadId: string | null;
  uploadUrl: string | null; offset: number; chunkSize: number | null; attempts: number; retryAt: number;
  lastError: string | null; updatedAt: number;
};
export type NewTransfer = Omit<Transfer, "state" | "uploadId" | "uploadUrl" | "offset" | "chunkSize" | "attempts" | "retryAt" | "lastError" | "updatedAt">;

let databasePromise: Promise<SQLite.SQLiteDatabase> | null = null;

async function database() {
  if (!databasePromise) {
    databasePromise = (async () => {
      const db = await SQLite.openDatabaseAsync("drivebound-transfer-queue.db");
      await db.execAsync(`
        PRAGMA journal_mode = WAL;
        CREATE TABLE IF NOT EXISTS transfers (
          local_version TEXT PRIMARY KEY, local_id TEXT NOT NULL, filename TEXT NOT NULL, media_type TEXT NOT NULL,
          mime_type TEXT NOT NULL, file_size INTEGER NOT NULL, created_at TEXT NOT NULL, modified_at TEXT NOT NULL,
          state TEXT NOT NULL, upload_id TEXT, upload_url TEXT, offset INTEGER NOT NULL DEFAULT 0,
          chunk_size INTEGER, attempts INTEGER NOT NULL DEFAULT 0, retry_at INTEGER NOT NULL DEFAULT 0,
          last_error TEXT, updated_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS transfers_pending ON transfers(state, retry_at, updated_at);
      `);
      await db.runAsync("UPDATE transfers SET state = 'retry', retry_at = 0 WHERE state = 'uploading'");
      return db;
    })();
  }
  return databasePromise;
}

function fromRow(row: Record<string, unknown>): Transfer {
  return {
    localVersion: String(row.local_version), localId: String(row.local_id), filename: String(row.filename), mediaType: String(row.media_type), mimeType: String(row.mime_type),
    fileSize: Number(row.file_size), createdAt: String(row.created_at), modifiedAt: String(row.modified_at), state: row.state as TransferState,
    uploadId: row.upload_id ? String(row.upload_id) : null, uploadUrl: row.upload_url ? String(row.upload_url) : null,
    offset: Number(row.offset), chunkSize: row.chunk_size === null ? null : Number(row.chunk_size), attempts: Number(row.attempts),
    retryAt: Number(row.retry_at), lastError: row.last_error ? String(row.last_error) : null, updatedAt: Number(row.updated_at),
  };
}

export async function enqueueTransfer(input: NewTransfer): Promise<void> {
  const db = await database();
  const now = Date.now();
  await db.runAsync(
    `INSERT INTO transfers (local_version, local_id, filename, media_type, mime_type, file_size, created_at, modified_at, state, offset, attempts, retry_at, updated_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, 0, 0, ?)
     ON CONFLICT(local_version) DO UPDATE SET local_id = excluded.local_id, filename = excluded.filename, media_type = excluded.media_type, mime_type = excluded.mime_type, file_size = excluded.file_size, created_at = excluded.created_at, modified_at = excluded.modified_at, updated_at = excluded.updated_at
     WHERE transfers.state != 'complete'`,
    input.localVersion, input.localId, input.filename, input.mediaType, input.mimeType, input.fileSize, input.createdAt, input.modifiedAt, now,
  );
}

export async function nextTransfer(): Promise<Transfer | null> {
  const db = await database();
  const row = await db.getFirstAsync<Record<string, unknown>>(
    "SELECT * FROM transfers WHERE state IN ('queued', 'retry', 'uploading') AND retry_at <= ? ORDER BY updated_at LIMIT 1", Date.now(),
  );
  return row ? fromRow(row) : null;
}

export async function updateTransfer(localVersion: string, changes: Partial<Pick<Transfer, "state" | "uploadId" | "uploadUrl" | "offset" | "chunkSize" | "attempts" | "retryAt" | "lastError">>): Promise<void> {
  const entries = Object.entries(changes);
  if (!entries.length) return;
  const db = await database();
  const fields = entries.map(([field]) => `${field.replace(/[A-Z]/g, char => `_${char.toLowerCase()}`)} = ?`).join(", ");
  await db.runAsync(`UPDATE transfers SET ${fields}, updated_at = ? WHERE local_version = ?`, ...entries.map(([, value]) => value), Date.now(), localVersion);
}

export async function queueSummary(): Promise<{ pending: number; failed: number; complete: number }> {
  const db = await database();
  const rows = await db.getAllAsync<{ state: TransferState; count: number }>("SELECT state, COUNT(*) AS count FROM transfers GROUP BY state");
  const values = { pending: 0, failed: 0, complete: 0 };
  for (const row of rows) {
    if (row.state === "complete") values.complete = Number(row.count);
    else if (row.state === "failed") values.failed = Number(row.count);
    else values.pending += Number(row.count);
  }
  return values;
}

export async function retryFailedTransfers(): Promise<void> {
  const db = await database();
  await db.runAsync("UPDATE transfers SET state = 'retry', retry_at = 0, last_error = NULL, updated_at = ? WHERE state = 'failed'", Date.now());
}

export async function pruneCompletedTransfers(before: number): Promise<void> {
  const db = await database();
  await db.runAsync("DELETE FROM transfers WHERE state = 'complete' AND updated_at < ?", before);
}
