import AsyncStorage from "@react-native-async-storage/async-storage";
import * as FileSystem from "expo-file-system/legacy";
import * as MediaLibrary from "expo-media-library/legacy";
import * as SecureStore from "expo-secure-store";
import { Platform } from "react-native";

export type BackupProgress = { scanned: number; uploaded: number; skipped: number; current?: string };
const DEVICE_TOKEN = "drivebound.deviceToken";
const SERVER_URL = "drivebound.serverUrl";
const BACKED_UP = "drivebound.backedUpIds";

export async function configureDevice(server: string, email: string, password: string, name: string, otp?: string) {
  const api = normalizeServerUrl(server);
  const body = new URLSearchParams({ username: email, password });
  if (otp) body.set("otp", otp);
  let login: Response;
  try {
    login = await fetch(`${api}/api/v1/auth/token`, { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" }, body: body.toString() });
  } catch (error) {
    throw new Error(`Cannot reach Drivebound at ${api}. ${String(error)}`);
  }
  if (!login.ok) throw new Error((await login.json()).detail ?? "Sign-in failed");
  const { access_token } = await login.json();
  const registration = await fetch(`${api}/api/v1/devices`, { method: "POST", headers: { Authorization: `Bearer ${access_token}`, "Content-Type": "application/json" }, body: JSON.stringify({ name, platform: Platform.OS }) });
  if (!registration.ok) throw new Error((await registration.json()).detail ?? "Device registration failed");
  const device = await registration.json();
  await SecureStore.setItemAsync(DEVICE_TOKEN, device.token);
  await SecureStore.setItemAsync(SERVER_URL, api);
  return device;
}

export function normalizeServerUrl(server: string): string {
  const value = server.trim().replace(/\/$/, "");
  try {
    const parsed = new URL(value);
    return parsed.toString().replace(/\/$/, "");
  } catch {
    throw new Error("Enter a complete server URL, such as http://10.0.2.2:8000");
  }
}

function base64Bytes(value: string): Uint8Array {
  const decoded = globalThis.atob(value);
  const bytes = new Uint8Array(decoded.length);
  for (let index = 0; index < decoded.length; index += 1) bytes[index] = decoded.charCodeAt(index);
  return bytes;
}

async function uploadAsset(server: string, token: string, asset: MediaLibrary.Asset) {
  const info = await MediaLibrary.getAssetInfoAsync(asset, { shouldDownloadFromNetwork: true });
  const uri = info.localUri ?? info.uri;
  const file = await FileSystem.getInfoAsync(uri);
  if (!file.exists || file.isDirectory) return false;
  const created = await fetch(`${server}/api/v1/uploads`, { method: "POST", headers: { "X-Device-Token": token, "Content-Type": "application/json" }, body: JSON.stringify({
    filename: asset.filename, mime_type: mimeFromFilename(asset.filename, asset.mediaType), total_size: file.size ?? 0,
    file_created_at: new Date(asset.creationTime).toISOString(), file_modified_at: new Date(asset.modificationTime).toISOString()
  }) });
  if (!created.ok) throw new Error(`Could not start backup for ${asset.filename}`);
  const session = await created.json();
  if (session.duplicate) return false;
  let offset = session.offset as number;
  while (offset < session.total_size) {
    const length = Math.min(session.chunk_size, session.total_size - offset);
    const encoded = await FileSystem.readAsStringAsync(uri, { encoding: FileSystem.EncodingType.Base64, position: offset, length });
    const response = await fetch(`${server}${session.upload_url}`, { method: "PATCH", headers: { "X-Device-Token": token, "Upload-Offset": String(offset), "Content-Type": "application/offset+octet-stream" }, body: base64Bytes(encoded).buffer as ArrayBuffer });
    if (!response.ok) throw new Error(`Backup interrupted for ${asset.filename}`);
    offset = (await response.json()).offset;
  }
  return true;
}

function mimeFromFilename(filename: string, mediaType: string): string {
  const extension = filename.split(".").pop()?.toLowerCase();
  const known: Record<string, string> = { jpg: "image/jpeg", jpeg: "image/jpeg", png: "image/png", heic: "image/heic", heif: "image/heif", webp: "image/webp", gif: "image/gif", mov: "video/quicktime", mp4: "video/mp4", m4v: "video/x-m4v", webm: "video/webm" };
  return (extension && known[extension]) || (mediaType === "video" ? "video/mp4" : "image/jpeg");
}

export async function runBackup(onProgress?: (progress: BackupProgress) => void): Promise<BackupProgress> {
  const permission = await MediaLibrary.requestPermissionsAsync(false, ["photo", "video"]);
  if (!permission.granted) throw new Error("Photo-library permission is required");
  const server = await SecureStore.getItemAsync(SERVER_URL);
  const token = await SecureStore.getItemAsync(DEVICE_TOKEN);
  if (!server || !token) throw new Error("Connect this device first");
  const completed = new Set<string>(JSON.parse((await AsyncStorage.getItem(BACKED_UP)) ?? "[]"));
  const progress: BackupProgress = { scanned: 0, uploaded: 0, skipped: 0 };
  let after: string | undefined;
  do {
    const page = await MediaLibrary.getAssetsAsync({ first: 100, after, mediaType: ["photo", "video"], sortBy: [MediaLibrary.SortBy.creationTime] });
    for (const asset of page.assets) {
      progress.scanned += 1; progress.current = asset.filename; onProgress?.({ ...progress });
      const localVersion = `${asset.id}:${asset.modificationTime}`;
      if (completed.has(localVersion)) progress.skipped += 1;
      else {
        const uploaded = await uploadAsset(server, token, asset);
        uploaded ? progress.uploaded += 1 : progress.skipped += 1;
        completed.add(localVersion);
        await AsyncStorage.setItem(BACKED_UP, JSON.stringify([...completed]));
      }
      onProgress?.({ ...progress });
    }
    after = page.hasNextPage ? page.endCursor : undefined;
  } while (after);
  delete progress.current;
  return progress;
}
