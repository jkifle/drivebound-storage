import * as FileSystem from "expo-file-system/legacy";
import * as MediaLibrary from "expo-media-library/legacy";
import * as SecureStore from "expo-secure-store";
import { Platform } from "react-native";

import {
  DEVICE_ID,
  DEVICE_TOKEN,
  SERVER_URL,
  connection,
  connectionScope,
  deviceFetch,
  normalizeServerUrl,
  reportBackupEvent,
} from "./api";
import { notifyBackupResult } from "./notifications";
import { backupPolicyBlocker, loadBackupPolicy, throttleForBandwidth } from "./policies";
import {
  acquireBackupRun,
  cancelPendingTransfers,
  claimNextTransfer,
  enqueueTransfer,
  listTransfers,
  queueSummary as readQueueSummary,
  releaseBackupRun,
  renewBackupRun,
  renewTransferLease,
  retryFailedTransfers,
  type NewTransfer,
  type QueueSummary,
  type Transfer,
  updateTransfer,
} from "./queue";

export type BackupProgress = {
  scanned: number;
  queued: number;
  known: number;
  uploaded: number;
  skipped: number;
  failed: number;
  deferred: number;
  current?: string;
  blocked?: string;
};

export type BackupRunMode = "manual" | "background";
export type BackupQueueSnapshot = { summary: QueueSummary; transfers: Transfer[] };
type UploadSession = { id: string; upload_url: string; offset: number; total_size: number; chunk_size: number; duplicate: boolean };
type UploadOutcome = { kind: "uploaded" } | { kind: "skipped" } | { kind: "blocked"; reason: string };

class TransferFailure extends Error {
  constructor(
    message: string,
    readonly retryable: boolean,
    readonly pauseQueue: boolean,
    readonly authenticationFailure = false,
  ) {
    super(message);
    this.name = "TransferFailure";
  }
}

export async function configureDevice(server: string, email: string, password: string, name: string, otp?: string) {
  const api = normalizeServerUrl(server);
  const body = new URLSearchParams({ username: email, password });
  if (otp) body.set("otp", otp);
  let login: Response;
  try {
    login = await fetch(`${api}/api/v1/auth/token`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: body.toString(),
    });
  } catch (error) {
    throw new Error(`Cannot reach Drivebound at ${api}. ${String(error)}`);
  }
  if (!login.ok) throw new Error(await responseMessage(login, "Sign-in failed"));
  const { access_token: accessToken } = await login.json() as { access_token: string };
  let registration: Response;
  try {
    registration = await fetch(`${api}/api/v1/devices`, {
      method: "POST",
      headers: { Authorization: `Bearer ${accessToken}`, "Content-Type": "application/json" },
      body: JSON.stringify({ name, platform: Platform.OS }),
    });
  } catch (error) {
    throw new Error(`Signed in, but device registration could not reach ${api}. ${String(error)}`);
  }
  if (!registration.ok) throw new Error(await responseMessage(registration, "Device registration failed"));
  const device = await registration.json() as { token: string; id: string };
  await Promise.all([
    SecureStore.setItemAsync(DEVICE_TOKEN, device.token),
    SecureStore.setItemAsync(SERVER_URL, api),
    SecureStore.setItemAsync(DEVICE_ID, device.id),
  ]);
  return device;
}

async function responseMessage(response: Response, fallback: string): Promise<string> {
  try {
    const body = await response.json() as { detail?: unknown };
    return typeof body.detail === "string" ? body.detail : fallback;
  } catch {
    return fallback;
  }
}

function base64Bytes(value: string): Uint8Array {
  const decoded = globalThis.atob(value);
  const bytes = new Uint8Array(decoded.length);
  for (let index = 0; index < decoded.length; index += 1) bytes[index] = decoded.charCodeAt(index);
  return bytes;
}

function mimeFromFilename(filename: string, mediaType: string): string {
  const extension = filename.split(".").pop()?.toLowerCase();
  const known: Record<string, string> = {
    jpg: "image/jpeg", jpeg: "image/jpeg", png: "image/png", heic: "image/heic", heif: "image/heif",
    webp: "image/webp", gif: "image/gif", mov: "video/quicktime", mp4: "video/mp4", m4v: "video/x-m4v", webm: "video/webm",
  };
  return (extension && known[extension]) || (mediaType === "video" ? "video/mp4" : "image/jpeg");
}

async function backupRequest(path: string, init: RequestInit = {}): Promise<Response> {
  try {
    return await deviceFetch(path, init);
  } catch (error) {
    throw new TransferFailure(String(error), true, true);
  }
}

async function requireSuccessfulResponse(response: Response, fallback: string): Promise<void> {
  if (response.ok) return;
  const message = await responseMessage(response, fallback);
  if (response.status === 401 || response.status === 403) {
    throw new TransferFailure(`${message}. Reconnect this phone in Drivebound settings.`, false, true, true);
  }
  if (response.status === 408 || response.status === 429 || response.status >= 500) {
    throw new TransferFailure(message, true, true);
  }
  throw new TransferFailure(message, false, false);
}

async function localFile(record: Transfer): Promise<{ uri: string; size: number }> {
  const info = await MediaLibrary.getAssetInfoAsync(record.localId, { shouldDownloadFromNetwork: true });
  const uri = info.localUri ?? info.uri;
  const file = await FileSystem.getInfoAsync(uri);
  if (!file.exists || file.isDirectory || typeof file.size !== "number") {
    throw new TransferFailure("The source media is no longer available on this phone", false, false);
  }
  return { uri, size: file.size };
}

async function withHeartbeat<T>(work: Promise<T>, heartbeat: () => Promise<void>): Promise<T> {
  let heartbeatError: unknown;
  let heartbeatRunning = false;
  const timer = setInterval(() => {
    if (heartbeatRunning) return;
    heartbeatRunning = true;
    heartbeat().catch(error => { heartbeatError = error; }).finally(() => { heartbeatRunning = false; });
  }, 30_000);
  try {
    const result = await work;
    if (heartbeatError) throw heartbeatError;
    return result;
  } finally {
    clearInterval(timer);
  }
}

async function resumeOrCreate(
  record: Transfer,
  fileSize: number,
  owner: string,
  heartbeat: () => Promise<void>,
): Promise<UploadSession> {
  if (record.uploadId && record.uploadUrl) {
    const status = await withHeartbeat(
      backupRequest(`/api/v1/uploads/${record.uploadId}`, { method: "HEAD" }),
      heartbeat,
    );
    if (status.ok && status.headers.get("Upload-Status") === "active") {
      const offset = Number(status.headers.get("Upload-Offset") ?? 0);
      const length = Number(status.headers.get("Upload-Length") ?? fileSize);
      if (length === fileSize && Number.isFinite(offset) && offset >= 0 && offset <= length) {
        const session = {
          id: record.uploadId,
          upload_url: record.uploadUrl,
          offset,
          total_size: length,
          chunk_size: record.chunkSize ?? 8 * 1024 * 1024,
          duplicate: false,
        };
        await updateTransfer(record.localVersion, { offset: session.offset, chunkSize: session.chunk_size }, owner);
        return session;
      }
    } else if (status.status !== 404 && status.status !== 410) {
      await requireSuccessfulResponse(status, `Could not resume ${record.filename}`);
    }
    await updateTransfer(record.localVersion, { uploadId: null, uploadUrl: null, offset: 0, chunkSize: null }, owner);
  }

  const created = await withHeartbeat(
    backupRequest("/api/v1/uploads", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        filename: record.filename,
        mime_type: record.mimeType,
        total_size: fileSize,
        file_created_at: record.createdAt,
        file_modified_at: record.modifiedAt,
      }),
    }),
    heartbeat,
  );
  await requireSuccessfulResponse(created, `Could not start backup for ${record.filename}`);
  const session = await created.json() as UploadSession;
  if (!session.duplicate) {
    await updateTransfer(record.localVersion, {
      uploadId: session.id,
      uploadUrl: session.upload_url,
      offset: session.offset,
      chunkSize: session.chunk_size,
    }, owner);
  }
  return session;
}

async function uploadTransfer(
  record: Transfer,
  owner: string,
  mode: BackupRunMode,
  onProgress: (current: string) => void,
  heartbeat: () => Promise<void>,
): Promise<UploadOutcome> {
  const policy = await loadBackupPolicy();
  const initialBlocker = await backupPolicyBlocker(policy, { mode });
  if (initialBlocker) return { kind: "blocked", reason: initialBlocker };
  const keepAlive = async () => {
    const [runRenewed, transferRenewed] = await Promise.all([
      heartbeat().then(() => true),
      renewTransferLease(record.localVersion, owner),
    ]);
    if (!runRenewed || !transferRenewed) throw new Error("Backup ownership expired; retry the backup");
  };
  await keepAlive();
  await updateTransfer(record.localVersion, { lastError: null }, owner);
  const file = await withHeartbeat(localFile(record), keepAlive);
  await updateTransfer(record.localVersion, { fileSize: file.size }, owner);
  const session = await resumeOrCreate(record, file.size, owner, keepAlive);
  if (session.duplicate) {
    await updateTransfer(record.localVersion, {
      state: "complete", offset: file.size, attempts: 0, retryAt: 0, lastError: null,
      leaseOwner: null, leaseExpiresAt: 0,
    }, owner);
    return { kind: "skipped" };
  }

  let offset = session.offset;
  while (offset < session.total_size) {
    const blocker = await backupPolicyBlocker(policy, { mode });
    if (blocker) {
      await updateTransfer(record.localVersion, {
        state: "retry", offset, retryAt: 0, lastError: blocker, leaseOwner: null, leaseExpiresAt: 0,
      }, owner);
      return { kind: "blocked", reason: blocker };
    }
    await keepAlive();
    const length = Math.min(session.chunk_size, session.total_size - offset);
    onProgress(record.filename);
    const encoded = await withHeartbeat(FileSystem.readAsStringAsync(file.uri, {
      encoding: FileSystem.EncodingType.Base64,
      position: offset,
      length,
    }), keepAlive);
    const response = await withHeartbeat(
      backupRequest(session.upload_url, {
        method: "PATCH",
        headers: { "Upload-Offset": String(offset), "Content-Type": "application/offset+octet-stream" },
        body: base64Bytes(encoded).buffer as ArrayBuffer,
      }),
      keepAlive,
    );
    if (!response.ok) {
      if (response.status === 409) {
        const status = await withHeartbeat(
          backupRequest(`/api/v1/uploads/${session.id}`, { method: "HEAD" }),
          keepAlive,
        );
        await requireSuccessfulResponse(status, `Could not reconcile ${record.filename}`);
        const remoteOffset = Number(status.headers.get("Upload-Offset"));
        if (Number.isFinite(remoteOffset) && remoteOffset >= 0 && remoteOffset <= session.total_size) {
          offset = remoteOffset;
          await updateTransfer(record.localVersion, { offset, lastError: null }, owner);
          continue;
        }
      }
      if (response.status === 404 || response.status === 410) {
        await updateTransfer(record.localVersion, { uploadId: null, uploadUrl: null, offset: 0, chunkSize: null }, owner);
        throw new TransferFailure("The upload session expired and will restart", true, false);
      }
      await requireSuccessfulResponse(response, `Backup interrupted for ${record.filename}`);
    }
    const payload = await response.json() as { offset?: number };
    const nextOffset = Number(payload.offset);
    if (!Number.isFinite(nextOffset) || nextOffset <= offset || nextOffset > session.total_size) {
      throw new TransferFailure("The server returned an invalid upload offset", true, true);
    }
    offset = nextOffset;
    await updateTransfer(record.localVersion, { offset, leaseExpiresAt: Date.now() + 5 * 60 * 1000 }, owner);
    await throttleForBandwidth(length, policy.bandwidthKbps, keepAlive);
  }
  await updateTransfer(record.localVersion, {
    state: "complete", offset, attempts: 0, retryAt: 0, lastError: null,
    leaseOwner: null, leaseExpiresAt: 0,
  }, owner);
  return { kind: "uploaded" };
}

async function scanLibrary(
  progress: BackupProgress,
  scope: string,
  owner: string,
  onProgress?: (value: BackupProgress) => void,
): Promise<void> {
  let after: string | undefined;
  do {
    const page = await MediaLibrary.getAssetsAsync({
      first: 100,
      after,
      mediaType: ["photo", "video"],
      sortBy: [MediaLibrary.SortBy.creationTime],
    });
    for (const asset of page.assets) {
      progress.scanned += 1;
      progress.current = asset.filename;
      const localAssetVersion = `${asset.id}:${asset.modificationTime}`;
      const transfer: NewTransfer = {
        localVersion: `${scope}:${localAssetVersion}`,
        connectionScope: scope,
        localId: asset.id,
        filename: asset.filename,
        mediaType: asset.mediaType,
        mimeType: mimeFromFilename(asset.filename, asset.mediaType),
        fileSize: 0,
        createdAt: new Date(asset.creationTime).toISOString(),
        modifiedAt: new Date(asset.modificationTime).toISOString(),
      };
      const enqueueResult = await enqueueTransfer(transfer);
      if (enqueueResult === "queued") progress.queued += 1;
      else if (enqueueResult === "complete") progress.known += 1;
      onProgress?.({ ...progress });
    }
    if (!await renewBackupRun(scope, owner)) throw new Error("Backup ownership expired; retry the backup");
    after = page.hasNextPage ? page.endCursor : undefined;
  } while (after);
}

function retryDelay(attempts: number): number {
  const base = Math.min(60 * 60 * 1000, 5_000 * (2 ** Math.min(attempts, 9)));
  return Math.round(base * (0.8 + Math.random() * 0.4));
}

export async function runBackup(
  onProgress?: (progress: BackupProgress) => void,
  options: { mode?: BackupRunMode } = {},
): Promise<BackupProgress> {
  const mode = options.mode ?? "manual";
  const scope = await connectionScope();
  const owner = `${mode}:${Date.now()}:${Math.random().toString(36).slice(2)}`;
  const progress: BackupProgress = { scanned: 0, queued: 0, known: 0, uploaded: 0, skipped: 0, failed: 0, deferred: 0 };
  if (!await acquireBackupRun(scope, owner)) return { ...progress, blocked: "A backup is already running" };

  try {
    const blocker = await backupPolicyBlocker(undefined, { mode });
    if (blocker) return { ...progress, blocked: blocker };
    const permission = mode === "background"
      ? await MediaLibrary.getPermissionsAsync(false, ["photo", "video"])
      : await MediaLibrary.requestPermissionsAsync(false, ["photo", "video"]);
    if (!permission.granted) throw new Error("Photo-library permission is required");
    await connection();
    await scanLibrary(progress, scope, owner, onProgress);

    for (;;) {
      const next = await claimNextTransfer(scope, owner);
      if (!next) break;
      progress.current = next.filename;
      onProgress?.({ ...progress });
      try {
        const outcome = await uploadTransfer(
          next,
          owner,
          mode,
          filename => {
            progress.current = filename;
            onProgress?.({ ...progress });
          },
          async () => {
            if (!await renewBackupRun(scope, owner)) throw new Error("Backup ownership expired; retry the backup");
          },
        );
        if (outcome.kind === "uploaded") progress.uploaded += 1;
        else if (outcome.kind === "skipped") progress.skipped += 1;
        else {
          progress.blocked = outcome.reason;
          break;
        }
      } catch (error) {
        const failure = error instanceof TransferFailure ? error : new TransferFailure(String(error), true, true);
        const attempts = next.attempts + 1;
        const permanentlyFailed = !failure.retryable || attempts >= 8;
        const message = failure.message.slice(0, 240);
        await updateTransfer(next.localVersion, {
          attempts,
          state: permanentlyFailed ? "failed" : "retry",
          retryAt: permanentlyFailed ? 0 : Date.now() + retryDelay(attempts),
          lastError: message,
          leaseOwner: null,
          leaseExpiresAt: 0,
        }, owner);
        if (permanentlyFailed) progress.failed += 1;
        else progress.deferred += 1;
        if (failure.pauseQueue || failure.authenticationFailure) {
          progress.blocked = message;
          break;
        }
      }
      onProgress?.({ ...progress });
    }
    delete progress.current;
    if (progress.failed) {
      await reportBackupEvent("failed", progress.uploaded, progress.skipped, `${progress.failed} media item${progress.failed === 1 ? "" : "s"} need attention.`);
      await notifyBackupResult("Backup needs attention", `${progress.failed} media item${progress.failed === 1 ? "" : "s"} could not be backed up.`);
    } else if (!progress.blocked && progress.uploaded + progress.skipped > 0) {
      await reportBackupEvent("completed", progress.uploaded, progress.skipped);
      await notifyBackupResult("Backup complete", `${progress.uploaded} uploaded, ${progress.skipped} already protected.`);
    }
    return progress;
  } finally {
    await releaseBackupRun(scope, owner);
  }
}

export async function backupQueueSnapshot(): Promise<BackupQueueSnapshot> {
  const scope = await connectionScope();
  const [summary, transfers] = await Promise.all([readQueueSummary(scope), listTransfers(scope)]);
  return { summary, transfers };
}

export async function retryFailedBackupTransfers(): Promise<void> {
  await retryFailedTransfers(await connectionScope());
}

export async function cancelPendingBackupTransfers(): Promise<number> {
  return cancelPendingTransfers(await connectionScope());
}
