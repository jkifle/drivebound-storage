import * as FileSystem from "expo-file-system/legacy";
import * as MediaLibrary from "expo-media-library/legacy";
import * as SecureStore from "expo-secure-store";

export const DEVICE_TOKEN = "drivebound.deviceToken";
export const SERVER_URL = "drivebound.serverUrl";
export const DEVICE_ID = "drivebound.deviceId";

export class DriveboundApiError extends Error {
  constructor(message: string, readonly status: number | null = null) {
    super(message);
    this.name = "DriveboundApiError";
  }
}

export type TimelineAsset = {
  id: string; mime_type: string; width: number | null; height: number | null; duration_seconds: number | null;
  taken_at: string | null; timeline_at: string; day: string; processing_status: string;
  original_url: string; thumbnail_url: string | null; original_filename: string | null;
  protection_status: string; restore_status: string;
};
export type TimelinePage = { items: TimelineAsset[]; next_cursor: string | null; has_more: boolean };
export type AssetDetail = {
  id: string;
  file_size: number;
  mime_type: string;
  width: number | null;
  height: number | null;
  duration_seconds: number | null;
  taken_at: string | null;
  file_created_at: string | null;
  file_modified_at: string | null;
  latitude: number | null;
  longitude: number | null;
  camera_make: string | null;
  camera_model: string | null;
  lens_model: string | null;
  original_filename: string | null;
  protection_status: string;
  processing_status: string;
};
export type Album = { id: string; name: string; description: string | null; asset_count: number; cover_thumbnail_url: string | null; role: "owner" | "editor" | "viewer" };
export type MapAsset = { id: string; latitude: number; longitude: number; taken_at: string | null; thumbnail_url: string | null; name: string };
export type ShareResult = { url: string; expires_at: string | null };

export function normalizeServerUrl(server: string): string {
  const value = server.trim().replace(/\/$/, "");
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    throw new Error("Enter a complete server URL, such as https://drivebound.example.com");
  }
  if (parsed.protocol !== "https:" && parsed.protocol !== "http:") {
    throw new Error("Drivebound server addresses must use http or https");
  }
  if (!__DEV__ && parsed.protocol !== "https:") {
    throw new Error("Production Drivebound connections require https");
  }
  if (parsed.username || parsed.password || parsed.search || parsed.hash) {
    throw new Error("Enter the server origin without credentials, a query, or a fragment");
  }
  parsed.pathname = parsed.pathname.replace(/\/$/, "");
  return parsed.toString().replace(/\/$/, "");
}

export async function connection() {
  const [server, token, deviceId] = await Promise.all([
    SecureStore.getItemAsync(SERVER_URL), SecureStore.getItemAsync(DEVICE_TOKEN), SecureStore.getItemAsync(DEVICE_ID),
  ]);
  if (!server || !token) throw new Error("Connect this device first");
  return { server, token, deviceId };
}

export async function connectionScope(): Promise<string> {
  const { deviceId } = await connection();
  if (!deviceId) throw new Error("Reconnect this device before starting a backup");
  return deviceId;
}

export async function isConnected(): Promise<boolean> {
  const [server, token] = await Promise.all([SecureStore.getItemAsync(SERVER_URL), SecureStore.getItemAsync(DEVICE_TOKEN)]);
  return Boolean(server && token);
}

export async function clearConnection() {
  await Promise.all([SecureStore.deleteItemAsync(SERVER_URL), SecureStore.deleteItemAsync(DEVICE_TOKEN), SecureStore.deleteItemAsync(DEVICE_ID)]);
}

function pathFor(server: string, path: string) { return path.startsWith("http") ? path : `${server}${path}`; }

export async function deviceFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const { server, token } = await connection();
  const headers = new Headers(init.headers);
  headers.set("X-Device-Token", token);
  try {
    return await fetch(pathFor(server, path), { ...init, headers });
  } catch (error) {
    throw new DriveboundApiError(`Cannot reach Drivebound at ${server}. Check the server address and connection. ${String(error)}`);
  }
}

export async function apiJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await deviceFetch(path, init);
  if (!response.ok) {
    let detail = "Drivebound request failed";
    try {
      const body = await response.json() as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
    } catch { /* response was not JSON */ }
    throw new DriveboundApiError(detail, response.status);
  }
  return response.json() as Promise<T>;
}

export async function timeline(cursor?: string, limit = 100): Promise<TimelinePage> {
  return apiJson<TimelinePage>(`/api/v1/assets?limit=${Math.max(1, Math.min(200, limit))}${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`);
}

export async function assetDetail(assetId: string): Promise<AssetDetail> {
  return apiJson<AssetDetail>(`/api/v1/assets/${assetId}`);
}

export async function verifyConnection(): Promise<void> {
  await timeline(undefined, 1);
}

export async function search(query: string): Promise<TimelineAsset[]> {
  if (!query.trim()) return [];
  return apiJson<TimelineAsset[]>(`/api/v1/search?query=${encodeURIComponent(query.trim())}`);
}

export async function memories(): Promise<TimelineAsset[]> { return apiJson<TimelineAsset[]>("/api/v1/memories"); }
export async function mapAssets(): Promise<MapAsset[]> { return apiJson<MapAsset[]>("/api/v1/map"); }
export async function albums(): Promise<Album[]> { return apiJson<Album[]>("/api/v1/albums"); }
export async function albumAssets(albumId: string): Promise<TimelineAsset[]> { return apiJson<TimelineAsset[]>(`/api/v1/albums/${albumId}/assets`); }

export async function createShare(assetId: string): Promise<ShareResult> {
  return apiJson<ShareResult>("/api/v1/shares", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ asset_id: assetId, allow_download: true, expires_in_hours: 168 }) });
}

export async function registerPushToken(token: string | null) {
  const response = await deviceFetch("/api/v1/devices/push-token", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token }) });
  if (!response.ok) throw new Error("Could not register notifications for this device");
}

export async function reportBackupEvent(kind: "completed" | "failed", uploaded: number, skipped: number, detail?: string) {
  try {
    await deviceFetch("/api/v1/devices/backup-events", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ kind, uploaded, skipped, detail }) });
  } catch { /* A backup must remain successful if push reporting is unavailable. */ }
}

export async function restoreToPhone(asset: TimelineAsset): Promise<void> {
  const permission = await MediaLibrary.requestPermissionsAsync(true, ["photo", "video"]);
  if (!permission.granted) throw new Error("Photo-library add permission is required to restore media");
  const { server, token } = await connection();
  const filename = (asset.original_filename || `${asset.id}.${asset.mime_type.startsWith("video/") ? "mp4" : "jpg"}`).replace(/[^A-Za-z0-9._-]/g, "_");
  const target = `${FileSystem.cacheDirectory ?? FileSystem.documentDirectory}drivebound-${Date.now()}-${filename}`;
  const result = await FileSystem.downloadAsync(pathFor(server, asset.original_url), target, { headers: { "X-Device-Token": token } });
  if (result.status < 200 || result.status >= 300) {
    await FileSystem.deleteAsync(result.uri, { idempotent: true });
    throw new DriveboundApiError(`Could not download the original (${result.status})`, result.status);
  }
  await MediaLibrary.saveToLibraryAsync(result.uri);
  await FileSystem.deleteAsync(result.uri, { idempotent: true });
}

function mediaCachePath(path: string): string {
  const cacheDirectory = FileSystem.cacheDirectory ?? FileSystem.documentDirectory;
  if (!cacheDirectory) throw new Error("Drivebound cannot access its local media cache");
  // Asset URLs contain UUIDs. Keep only filesystem-safe characters so Android can
  // render the cached response without depending on the remote URL's extension.
  const safeName = path.replace(/[^A-Za-z0-9]/g, "_").slice(-180);
  return `${cacheDirectory}drivebound-preview-${safeName}.media`;
}

/**
 * Download an authenticated media response into the app cache for native image
 * rendering. Android's Image loader does not reliably preserve custom headers
 * through every redirect/cache path, while FileSystem.downloadAsync does.
 */
export async function cachedMediaUri(path: string): Promise<string> {
  const { server, token } = await connection();
  const target = mediaCachePath(`${server}${path}`);
  const cached = await FileSystem.getInfoAsync(target);
  if (cached.exists && cached.size > 0) return cached.uri;

  const result = await FileSystem.downloadAsync(pathFor(server, path), target, {
    headers: { "X-Device-Token": token },
  });
  if (result.status < 200 || result.status >= 300) {
    await FileSystem.deleteAsync(result.uri, { idempotent: true });
    throw new Error(`Could not download media preview (${result.status})`);
  }
  return result.uri;
}

export async function mediaUrl(path: string): Promise<{ uri: string; headers: Record<string, string> }> {
  const { server, token } = await connection();
  return { uri: pathFor(server, path), headers: { "X-Device-Token": token } };
}
