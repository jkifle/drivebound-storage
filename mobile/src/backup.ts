import * as FileSystem from "expo-file-system/legacy";
import * as MediaLibrary from "expo-media-library/legacy";
import * as SecureStore from "expo-secure-store";
import { Platform } from "react-native";

import { DEVICE_ID, DEVICE_TOKEN, SERVER_URL, connection, normalizeServerUrl, reportBackupEvent } from "./api";
import { notifyBackupResult } from "./notifications";
import { backupPolicyBlocker, loadBackupPolicy, throttleForBandwidth } from "./policies";
import { enqueueTransfer, nextTransfer, pruneCompletedTransfers, queueSummary, type NewTransfer, type Transfer, updateTransfer } from "./queue";

export type BackupProgress = { scanned: number; queued: number; uploaded: number; skipped: number; failed: number; current?: string; blocked?: string };
type UploadSession = { id: string; upload_url: string; offset: number; total_size: number; chunk_size: number; duplicate: boolean };

export async function configureDevice(server: string, email: string, password: string, name: string, otp?: string) {
  const api = normalizeServerUrl(server);
  const body = new URLSearchParams({ username: email, password });
  if (otp) body.set("otp", otp);
  let login: Response;
  try { login = await fetch(`${api}/api/v1/auth/token`, { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" }, body: body.toString() }); }
  catch (error) { throw new Error(`Cannot reach Drivebound at ${api}. ${String(error)}`); }
  if (!login.ok) throw new Error((await login.json()).detail ?? "Sign-in failed");
  const { access_token } = await login.json();
  const registration = await fetch(`${api}/api/v1/devices`, {
    method: "POST", headers: { Authorization: `Bearer ${access_token}`, "Content-Type": "application/json" }, body: JSON.stringify({ name, platform: Platform.OS }),
  });
  if (!registration.ok) throw new Error((await registration.json()).detail ?? "Device registration failed");
  const device = await registration.json();
  await Promise.all([
    SecureStore.setItemAsync(DEVICE_TOKEN, device.token), SecureStore.setItemAsync(SERVER_URL, api), SecureStore.setItemAsync(DEVICE_ID, device.id),
  ]);
  return device;
}

function base64Bytes(value: string): Uint8Array {
  const decoded = globalThis.atob(value);
  const bytes = new Uint8Array(decoded.length);
  for (let index = 0; index < decoded.length; index += 1) bytes[index] = decoded.charCodeAt(index);
  return bytes;
}

function mimeFromFilename(filename: string, mediaType: string): string {
  const extension = filename.split(".").pop()?.toLowerCase();
  const known: Record<string, string> = { jpg: "image/jpeg", jpeg: "image/jpeg", png: "image/png", heic: "image/heic", heif: "image/heif", webp: "image/webp", gif: "image/gif", mov: "video/quicktime", mp4: "video/mp4", m4v: "video/x-m4v", webm: "video/webm" };
  return (extension && known[extension]) || (mediaType === "video" ? "video/mp4" : "image/jpeg");
}

async function request(path: string, init: RequestInit = {}) {
  const { server, token } = await connection();
  const headers = new Headers(init.headers);
  headers.set("X-Device-Token", token);
  return fetch(path.startsWith("http") ? path : `${server}${path}`, { ...init, headers });
}

async function localFile(record: Transfer): Promise<{ uri: string; size: number }> {
  const info = await MediaLibrary.getAssetInfoAsync(record.localId, { shouldDownloadFromNetwork: true });
  const uri = info.localUri ?? info.uri;
  const file = await FileSystem.getInfoAsync(uri);
  if (!file.exists || file.isDirectory || typeof file.size !== "number") throw new Error("The source media is no longer available on this phone");
  return { uri, size: file.size };
}

async function resumeOrCreate(record: Transfer, fileSize: number): Promise<UploadSession> {
  if (record.uploadId && record.uploadUrl) {
    const status = await request(`/api/v1/uploads/${record.uploadId}`, { method: "HEAD" });
    if (status.ok && status.headers.get("Upload-Status") === "active") {
      const offset = Number(status.headers.get("Upload-Offset") ?? 0);
      const length = Number(status.headers.get("Upload-Length") ?? fileSize);
      if (length === fileSize && Number.isFinite(offset)) {
        const session = { id: record.uploadId, upload_url: record.uploadUrl, offset, total_size: length, chunk_size: record.chunkSize ?? 8 * 1024 * 1024, duplicate: false };
        await updateTransfer(record.localVersion, { offset: session.offset, chunkSize: session.chunk_size });
        return session;
      }
    }
    await updateTransfer(record.localVersion, { uploadId: null, uploadUrl: null, offset: 0, chunkSize: null });
  }
  const created = await request("/api/v1/uploads", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
      filename: record.filename, mime_type: record.mimeType, total_size: fileSize, file_created_at: record.createdAt, file_modified_at: record.modifiedAt,
    }),
  });
  if (!created.ok) throw new Error(`Could not start backup for ${record.filename}`);
  const session = await created.json() as UploadSession;
  if (!session.duplicate) await updateTransfer(record.localVersion, { uploadId: session.id, uploadUrl: session.upload_url, offset: session.offset, chunkSize: session.chunk_size });
  return session;
}

async function uploadTransfer(record: Transfer, onProgress: (current: string) => void): Promise<"uploaded" | "skipped" | "blocked"> {
  const policy = await loadBackupPolicy();
  const initialBlocker = await backupPolicyBlocker(policy);
  if (initialBlocker) return "blocked";
  await updateTransfer(record.localVersion, { state: "uploading", lastError: null });
  const file = await localFile(record);
  const session = await resumeOrCreate(record, file.size);
  if (session.duplicate) {
    await updateTransfer(record.localVersion, { state: "complete", offset: file.size, lastError: null });
    return "skipped";
  }
  let offset = session.offset;
  while (offset < session.total_size) {
    const blocker = await backupPolicyBlocker(policy);
    if (blocker) {
      await updateTransfer(record.localVersion, { state: "retry", offset, retryAt: 0, lastError: blocker });
      return "blocked";
    }
    const length = Math.min(session.chunk_size, session.total_size - offset);
    onProgress(record.filename);
    const encoded = await FileSystem.readAsStringAsync(file.uri, { encoding: FileSystem.EncodingType.Base64, position: offset, length });
    const response = await request(session.upload_url, {
      method: "PATCH",
      headers: { "Upload-Offset": String(offset), "Content-Type": "application/offset+octet-stream" },
      body: base64Bytes(encoded).buffer as ArrayBuffer,
    });
    if (!response.ok) {
      if (response.status === 409) {
        const status = await request(`/api/v1/uploads/${session.id}`, { method: "HEAD" });
        const remoteOffset = Number(status.headers.get("Upload-Offset"));
        if (status.ok && Number.isFinite(remoteOffset) && remoteOffset >= 0 && remoteOffset <= session.total_size) {
          offset = remoteOffset;
          await updateTransfer(record.localVersion, { offset, lastError: null });
          continue;
        }
      }
      if (response.status === 410) await updateTransfer(record.localVersion, { uploadId: null, uploadUrl: null, offset: 0, chunkSize: null });
      throw new Error(`Backup interrupted for ${record.filename}`);
    }
    offset = Number((await response.json()).offset);
    await updateTransfer(record.localVersion, { offset });
    await throttleForBandwidth(length, policy.bandwidthKbps);
  }
  await updateTransfer(record.localVersion, { state: "complete", offset, lastError: null });
  return "uploaded";
}

async function scanLibrary(progress: BackupProgress, onProgress?: (value: BackupProgress) => void) {
  let after: string | undefined;
  do {
    const page = await MediaLibrary.getAssetsAsync({ first: 100, after, mediaType: ["photo", "video"], sortBy: [MediaLibrary.SortBy.creationTime] });
    for (const asset of page.assets) {
      progress.scanned += 1;
      progress.current = asset.filename;
      const transfer: NewTransfer = {
        localVersion: `${asset.id}:${asset.modificationTime}`, localId: asset.id, filename: asset.filename, mediaType: asset.mediaType,
        mimeType: mimeFromFilename(asset.filename, asset.mediaType), fileSize: 0,
        createdAt: new Date(asset.creationTime).toISOString(), modifiedAt: new Date(asset.modificationTime).toISOString(),
      };
      await enqueueTransfer(transfer);
      progress.queued += 1;
      onProgress?.({ ...progress });
    }
    after = page.hasNextPage ? page.endCursor : undefined;
  } while (after);
}

function retryDelay(attempts: number) { return Math.min(60 * 60 * 1000, 5_000 * (2 ** Math.min(attempts, 9))); }

export async function runBackup(onProgress?: (progress: BackupProgress) => void): Promise<BackupProgress> {
  const permission = await MediaLibrary.requestPermissionsAsync(false, ["photo", "video"]);
  if (!permission.granted) throw new Error("Photo-library permission is required");
  await connection();
  const progress: BackupProgress = { scanned: 0, queued: 0, uploaded: 0, skipped: 0, failed: 0 };
  const blocker = await backupPolicyBlocker();
  if (blocker) return { ...progress, blocked: blocker };
  await scanLibrary(progress, onProgress);
  await pruneCompletedTransfers(Date.now() - 45 * 24 * 60 * 60 * 1000);
  for (;;) {
    const next = await nextTransfer();
    if (!next) break;
    progress.current = next.filename;
    onProgress?.({ ...progress });
    try {
      const outcome = await uploadTransfer(next, filename => { progress.current = filename; onProgress?.({ ...progress }); });
      if (outcome === "uploaded") progress.uploaded += 1;
      else if (outcome === "skipped") progress.skipped += 1;
      else { progress.blocked = (await backupPolicyBlocker()) ?? "Backup paused"; break; }
    } catch (error) {
      const attempts = next.attempts + 1;
      const message = String(error).slice(0, 240);
      await updateTransfer(next.localVersion, {
        attempts, state: attempts >= 8 ? "failed" : "retry", retryAt: attempts >= 8 ? 0 : Date.now() + retryDelay(attempts), lastError: message,
      });
      progress.failed += 1;
    }
    onProgress?.({ ...progress });
  }
  delete progress.current;
  if (progress.failed) {
    await reportBackupEvent("failed", progress.uploaded, progress.skipped, `${progress.failed} media item${progress.failed === 1 ? "" : "s"} need attention.`);
    await notifyBackupResult("Backup needs attention", `${progress.failed} media item${progress.failed === 1 ? "" : "s"} could not be backed up.`);
  } else if (!progress.blocked) {
    await reportBackupEvent("completed", progress.uploaded, progress.skipped);
    await notifyBackupResult("Backup complete", `${progress.uploaded} uploaded, ${progress.skipped} already protected.`);
  }
  return progress;
}

export { queueSummary };
