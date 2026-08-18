"use client";

import { useVirtualizer } from "@tanstack/react-virtual";
import {
  ChangeEvent,
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";
import type { DriveboundUser } from "./auth-gate";
import { API_URL, apiRequest } from "./api-client";
import { LibraryScreen, type LibraryView } from "./library-screens";
type TimelineAsset = {
  id: string;
  mime_type: string;
  width: number | null;
  height: number | null;
  duration_seconds: number | null;
  taken_at: string | null;
  timeline_at: string;
  day: string;
  processing_status: string;
  original_url: string;
  thumbnail_url: string | null;
  original_filename?: string | null;
  protection_status: string;
  restore_status: string;
};

type TimelineResponse = {
  items: TimelineAsset[];
  next_cursor: string | null;
  has_more: boolean;
};

type AssetDetail = TimelineAsset & {
  camera_make?: string | null;
  camera_model?: string | null;
  latitude?: number | null;
  longitude?: number | null;
  file_size?: number;
  file_created_at?: string | null;
  file_modified_at?: string | null;
  lens_model?: string | null;
  orientation?: number | null;
};

type UploadItem = {
  id: string;
  name: string;
  progress: number;
  state: "waiting" | "uploading" | "complete" | "duplicate" | "failed";
  error?: string;
};

type StorageDrive = {
  name: string;
  path: string;
  role: string;
  status: string;
  smart_status: string;
  writable: boolean;
  total_bytes?: number;
  used_bytes?: number;
  free_bytes?: number;
  used_percent?: number;
};

type StorageStatus = {
  status: string;
  drives: StorageDrive[];
  smart_available: boolean;
  health_note: string;
};

type ProtectionStatus = { total: number; protected: number; queued: number; unprotected: number; failed: number };
type MonitoringOverview = { open_events: number; failed_assets: number; unprotected_assets: number };
type NodeInventoryItem = {
  name: string;
  type: "directory" | "file";
  path?: string;
  children_count?: number;
};

type NodeMount = {
  root: string;
  available: boolean;
  total_bytes?: number;
  used_bytes?: number;
  free_bytes?: number;
  reason?: string;
  inventory?: {
    root: string;
    available: boolean;
    total_items?: number;
    truncated?: boolean;
    error?: string;
    items?: NodeInventoryItem[];
  };
};

type NodeDirectoryEntry = {
  name: string;
  path: string;
  type: "directory" | "file";
  size?: number | null;
};

type NodeDirectoryListing = {
  path: string;
  exists: boolean;
  is_dir?: boolean;
  root?: string;
  items?: NodeDirectoryEntry[];
  total_items?: number;
  truncated?: boolean;
  error?: string;
};

type PairedNode = {
  id: string;
  name: string;
  status: string;
  last_seen_at: string | null;
  capabilities?: {
    storage?: boolean;
    backup?: boolean;
    service_version?: string;
    mounts?: NodeMount[];
  } | null;
};

type ExternalLibrary = {
  id: string;
  name: string;
  path: string;
  status: string;
  file_count: number;
  last_scanned_at: string | null;
  error: string | null;
};

type TimelineRow =
  | { kind: "heading"; day: string; id: string }
  | { kind: "photos"; assets: TimelineAsset[]; id: string };

function DirectoryPathField({ value, onChange }: { value: string; onChange: (next: string) => void }) {
  const pickerId = useId();
  const [pickerKey, setPickerKey] = useState(0);

  const handleSelect = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) return;
    const relative = (file as File & { webkitRelativePath?: string }).webkitRelativePath;
    const folder = relative ? relative.split("/").slice(0, -1).join("/") : file.name;
    onChange(folder || value || "/data/imports");
    setPickerKey((current) => current + 1);
  };

  return (
    <>
      <div className="folder-input-wrap">
        <input value={value} onChange={(event) => onChange(event.target.value)} aria-label="Media folder path" />
        <button
          type="button"
          className="secondary-button"
          onClick={() => {
            const input = document.getElementById(pickerId) as HTMLInputElement | null;
            if (input) input.click();
          }}
        >
          Choose folder
        </button>
      </div>
      <input
        key={pickerKey}
        id={pickerId}
        type="file"
        hidden
        webkitdirectory="true"
        directory="true"
        multiple={false}
        onChange={handleSelect}
      />
    </>
  );
}

function apiUrl(path: string) {
  return path.startsWith("http") ? path : `${API_URL}${path}`;
}

function apiFetch(path: string, init?: RequestInit) {
  return apiRequest(path, init);
}

function formatDay(day: string) {
  const date = new Date(`${day}T12:00:00Z`);
  const today = new Date();
  const yesterday = new Date(today);
  yesterday.setDate(today.getDate() - 1);
  const sameDay = (candidate: Date, reference: Date) =>
    candidate.getUTCFullYear() === reference.getFullYear() &&
    candidate.getUTCMonth() === reference.getMonth() &&
    candidate.getUTCDate() === reference.getDate();
  if (sameDay(date, today)) return "Today";
  if (sameDay(date, yesterday)) return "Yesterday";
  return new Intl.DateTimeFormat(undefined, {
    weekday: "long",
    month: "long",
    day: "numeric",
    year: date.getUTCFullYear() === today.getFullYear() ? undefined : "numeric",
    timeZone: "UTC",
  }).format(date);
}

function formatBytes(bytes?: number) {
  if (!bytes) return "—";
  const units = ["B", "KB", "MB", "GB"];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / 1024 ** index).toFixed(index ? 1 : 0)} ${units[index]}`;
}

function PhotoTile({ asset, onOpen }: { asset: TimelineAsset; onOpen: () => void }) {
  const [failed, setFailed] = useState(false);
  const ready = asset.processing_status === "complete" && asset.thumbnail_url && !failed;
  return (
    <button className="photo-tile" onClick={onOpen} aria-label={`Open photo from ${formatDay(asset.day)}`}>
      {ready ? (
        <img
          src={apiUrl(asset.thumbnail_url!)}
          alt=""
          loading="lazy"
          onError={() => setFailed(true)}
        />
      ) : (
        <span className={`photo-state state-${asset.processing_status}`}>
          <span className="photo-state-mark" aria-hidden="true">
            {asset.processing_status === "failed" ? "!" : "◌"}
          </span>
          {asset.processing_status === "failed" ? "Couldn’t process" : "Preparing"}
        </span>
      )}
      {asset.mime_type.startsWith("video/") && <span className="video-badge">▶</span>}
    </button>
  );
}

function Viewer({
  asset,
  assets,
  onClose,
  onSelect,
}: {
  asset: TimelineAsset;
  assets: TimelineAsset[];
  onClose: () => void;
  onSelect: (asset: TimelineAsset) => void;
}) {
  const [detail, setDetail] = useState<AssetDetail | null>(null);
  const [actionMessage, setActionMessage] = useState<string | null>(null);
  const [shareUrl, setShareUrl] = useState<string | null>(null);
  const index = assets.findIndex((item) => item.id === asset.id);
  const move = useCallback(
    (direction: number) => {
      const next = assets[index + direction];
      if (next) onSelect(next);
    },
    [assets, index, onSelect],
  );

  useEffect(() => {
    apiFetch(`/api/v1/assets/${asset.id}`)
      .then((response) => (response.ok ? response.json() : Promise.reject()))
      .then(setDetail)
      .catch(() => undefined);
  }, [asset.id]);

  useEffect(() => {
    const handleKey = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") onClose();
      if (event.key === "ArrowLeft") move(-1);
      if (event.key === "ArrowRight") move(1);
    };
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [move, onClose]);

  const protect = async () => {
    setActionMessage("Creating a verified copy…");
    const response = await apiFetch(`/api/v1/assets/${asset.id}/protect`, { method: "POST" });
    setActionMessage(response.ok ? "Protection copy queued." : "Could not queue protection.");
  };

  const restore = async () => {
    setActionMessage("Restoring the original from its verified copy…");
    const response = await apiFetch(`/api/v1/assets/${asset.id}/restore`, { method: "POST" });
    const result = await response.json().catch(() => null);
    setActionMessage(response.ok ? "Restore queued. The original timestamp will be reapplied." : (result?.detail ?? "Restore could not be queued."));
  };

  const share = async () => {
    setActionMessage("Creating private linkâ€¦");
    const response = await apiFetch(`/api/v1/shares`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ asset_id: asset.id, expires_in_hours: 168 }),
    });
    if (!response.ok) {
      setActionMessage("Could not create a link.");
      return;
    }
    const result = await response.json();
    setShareUrl(result.url);
    try {
      await navigator.clipboard.writeText(result.url);
      setActionMessage("Private link copied. It expires in 7 days.");
    } catch {
      setActionMessage("Private link created. Copy it from the field below.");
    }
  };

  return (
    <div className="viewer" role="dialog" aria-modal="true" aria-label="Photo viewer">
      <div className="viewer-bar">
        <button className="icon-button" onClick={onClose} aria-label="Close viewer">×</button>
        <div className="viewer-title">
          <strong>{formatDay(asset.day)}</strong>
          <span>{new Date(asset.timeline_at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}</span>
        </div>
        <a className="viewer-download" href={apiUrl(asset.original_url)} download>
          Download original
        </a>
      </div>
      <div className="viewer-stage">
        <button className="viewer-arrow left" onClick={() => move(-1)} disabled={index <= 0} aria-label="Previous photo">‹</button>
        {asset.mime_type.startsWith("video/") ? (
          // User-provided videos do not have an associated captions source.
          // eslint-disable-next-line jsx-a11y/media-has-caption
          <video src={apiUrl(asset.original_url)} controls autoPlay />
        ) : (
          <img src={apiUrl(asset.original_url)} alt={`Memory from ${formatDay(asset.day)}`} />
        )}
        <button className="viewer-arrow right" onClick={() => move(1)} disabled={index >= assets.length - 1} aria-label="Next photo">›</button>
      </div>
      <aside className="viewer-details">
        <span className="eyebrow">Details</span>
        <dl>
          <div><dt>Captured</dt><dd>{new Date(asset.timeline_at).toLocaleString()}</dd></div>
          <div><dt>Dimensions</dt><dd>{asset.width && asset.height ? `${asset.width} × ${asset.height}` : "Processing"}</dd></div>
          <div><dt>Camera</dt><dd>{[detail?.camera_make, detail?.camera_model].filter(Boolean).join(" ") || "—"}</dd></div>
          <div><dt>Lens</dt><dd>{detail?.lens_model || "—"}</dd></div>
          <div><dt>File modified</dt><dd>{detail?.file_modified_at ? new Date(detail.file_modified_at).toLocaleString() : "—"}</dd></div>
          <div><dt>File size</dt><dd>{formatBytes(detail?.file_size)}</dd></div>
          {detail?.latitude != null && detail?.longitude != null && (
            <div><dt>Location</dt><dd>{detail.latitude.toFixed(4)}, {detail.longitude.toFixed(4)}</dd></div>
          )}
        </dl>
        <div className="asset-actions">
          <button onClick={() => void protect()} disabled={asset.protection_status === "protected"}>
            {asset.protection_status === "protected" ? "Protected" : "Protect on backup drive"}
          </button>
          <button onClick={() => void restore()} disabled={asset.protection_status !== "protected" || asset.restore_status === "restoring" || asset.restore_status === "queued"}>
            {asset.restore_status === "restoring" || asset.restore_status === "queued" ? "Restore in progress" : "Restore from protected copy"}
          </button>
          <button onClick={() => void share()}>Copy private link</button>
          {shareUrl && <input className="share-url" aria-label="Private share link" readOnly value={shareUrl} onFocus={(event) => event.currentTarget.select()} />}
          {actionMessage && <p aria-live="polite">{actionMessage}</p>}
        </div>
      </aside>
    </div>
  );
}

function UploadPanel({ initialFiles = [], onClose, onFinished }: { initialFiles?: File[]; onClose: () => void; onFinished: () => void }) {
  const [uploads, setUploads] = useState<UploadItem[]>([]);
  const [dragging, setDragging] = useState(false);
  const initialFilesStarted = useRef(false);
  const active = uploads.some((item) => item.state === "waiting" || item.state === "uploading");

  const runUpload = useCallback(async (file: File, id: string) => {
    try {
      const create = await apiFetch(`/api/v1/uploads`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          filename: file.name,
          mime_type: file.type || "application/octet-stream",
          total_size: file.size,
          file_modified_at: file.lastModified ? new Date(file.lastModified).toISOString() : null,
        }),
      });
      if (!create.ok) {
        const problem = await create.json().catch(() => null);
        throw new Error(problem?.detail ?? "Could not start upload");
      }
      let upload = await create.json();
      if (upload.duplicate) {
        setUploads((current) => current.map((item) =>
          item.id === id ? { ...item, progress: 100, state: "duplicate" } : item,
        ));
        return;
      }
      let offset = Number(upload.offset);
      const chunkSize = Number(upload.chunk_size);
      while (offset < file.size) {
        const chunkOffset = offset;
        const chunk = file.slice(offset, Math.min(offset + chunkSize, file.size));
        let response: Response | null = null;
        let recovered = false;
        for (let attempt = 0; attempt < 3; attempt += 1) {
          try {
            response = await apiFetch(upload.upload_url, {
              method: "PATCH",
              headers: { "Upload-Offset": String(offset), "Content-Type": "application/offset+octet-stream" },
              body: chunk,
            });
            if (response.ok) break;
          } catch {
            // A HEAD request below recovers the authoritative server offset.
          }
          const status = await apiFetch(upload.upload_url, { method: "HEAD" });
          if (status.ok) {
            const serverOffset = Number(status.headers.get("Upload-Offset") ?? chunkOffset);
            if (serverOffset > chunkOffset) {
              offset = serverOffset;
              recovered = true;
              break;
            }
          }
          if (attempt < 2) await new Promise((resolve) => window.setTimeout(resolve, 500 * (attempt + 1)));
        }
        if (recovered) continue;
        if (!response?.ok) throw new Error("Upload interrupted");
        upload = await response.json();
        offset = Number(upload.offset);
        setUploads((current) => current.map((item) =>
          item.id === id
            ? { ...item, progress: Math.round((offset / file.size) * 100), state: "uploading" }
            : item,
        ));
      }
      setUploads((current) => current.map((item) =>
        item.id === id ? { ...item, progress: 100, state: upload.duplicate ? "duplicate" : "complete" } : item,
      ));
    } catch (reason) {
      setUploads((current) => current.map((item) =>
        item.id === id ? { ...item, state: "failed", error: reason instanceof Error ? reason.message : "Upload failed" } : item,
      ));
    }
  }, []);

  const addFiles = useCallback(async (files: File[]) => {
    if (!files.length) return;
    const queued = files.map((file) => ({
      id: crypto.randomUUID(),
      name: file.name,
      progress: 0,
      state: "waiting" as const,
    }));
    setUploads((current) => [...current, ...queued]);
    const queue = files.map((file, index) => ({ file, id: queued[index].id }));
    const workers = Array.from({ length: Math.min(3, queue.length) }, async () => {
      while (queue.length) {
        const next = queue.shift();
        if (next) await runUpload(next.file, next.id);
      }
    });
    await Promise.all(workers);
    onFinished();
  }, [onFinished, runUpload]);

  useEffect(() => {
    if (initialFilesStarted.current || !initialFiles.length) return;
    initialFilesStarted.current = true;
    void addFiles(initialFiles);
  }, [addFiles, initialFiles]);

  const chooseFiles = (event: ChangeEvent<HTMLInputElement>) => {
    void addFiles(Array.from(event.target.files ?? []));
    event.target.value = "";
  };

  return (
    <div className="upload-scrim" role="dialog" aria-modal="true" aria-labelledby="upload-title">
      <section className="upload-panel">
        <div className="upload-heading">
          <div><span className="eyebrow">Resumable transfer</span><h2 id="upload-title">Add to Drivebound</h2></div>
          <button className="icon-button dark" onClick={onClose} disabled={active} aria-label="Close upload panel">×</button>
        </div>
        <label
          className={`drop-zone ${dragging ? "dragging" : ""}`}
          onDragEnter={(event) => { event.preventDefault(); event.stopPropagation(); setDragging(true); }}
          onDragOver={(event) => { event.preventDefault(); event.stopPropagation(); event.dataTransfer.dropEffect = "copy"; setDragging(true); }}
          onDragLeave={(event) => { event.preventDefault(); event.stopPropagation(); setDragging(false); }}
          onDrop={(event) => {
            event.preventDefault();
            event.stopPropagation();
            setDragging(false);
            void addFiles(Array.from(event.dataTransfer.files));
          }}
        >
          <input type="file" accept="image/*,video/*" multiple onChange={chooseFiles} />
          <span className="upload-mark" aria-hidden="true">↑</span>
          <strong>Drop photos and videos here</strong>
          <span>Transfers resume in chunks and originals are checksum verified</span>
        </label>
        {uploads.length > 0 && (
          <div className="upload-list" aria-live="polite">
            {uploads.map((item) => (
              <div className="upload-row" key={item.id}>
                <div className="upload-file"><strong>{item.name}</strong><span>{item.error ?? item.state}</span></div>
                <div className="progress-track"><span style={{ width: `${item.progress}%` }} /></div>
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

function StoragePanel({ onClose, onLibraryChanged }: { onClose: () => void; onLibraryChanged: () => void }) {
  const [storage, setStorage] = useState<StorageStatus | null>(null);
  const [libraries, setLibraries] = useState<ExternalLibrary[]>([]);
  const [protection, setProtection] = useState<ProtectionStatus | null>(null);
  const [name, setName] = useState("Imported drive");
  const [path, setPath] = useState("/data/imports");
  const [message, setMessage] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [monitoring, setMonitoring] = useState<MonitoringOverview | null>(null);
  const [nodes, setNodes] = useState<PairedNode[]>([]);
  const [pairingCode, setPairingCode] = useState<string | null>(null);
  const [nodeBrowse, setNodeBrowse] = useState<Record<string, NodeDirectoryListing>>({});

  const refresh = useCallback(async () => {
    const [storageResponse, libraryResponse, protectionResponse, monitoringResponse, nodesResponse] = await Promise.all([
      apiFetch(`/api/v1/storage`),
      apiFetch(`/api/v1/libraries`),
      apiFetch(`/api/v1/assets/protection/status`),
      apiFetch(`/api/v1/monitoring`),
      apiFetch(`/api/v1/nodes`),
    ]);
    if (storageResponse.ok) setStorage(await storageResponse.json());
    if (libraryResponse.ok) setLibraries(await libraryResponse.json());
    if (protectionResponse.ok) setProtection(await protectionResponse.json());
    if (monitoringResponse.ok) setMonitoring(await monitoringResponse.json());
    if (nodesResponse.ok) setNodes(await nodesResponse.json());
  }, []);

  useEffect(() => {
    const initial = window.setTimeout(() => void refresh(), 0);
    const interval = window.setInterval(() => void refresh(), 5000);
    return () => { window.clearTimeout(initial); window.clearInterval(interval); };
  }, [refresh]);

  const addLibrary = async () => {
    setSaving(true);
    setMessage(null);
    try {
      const response = await apiFetch(`/api/v1/libraries`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, path }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail ?? "Could not add library");
      await apiFetch(`/api/v1/libraries/${body.id}/scan`, { method: "POST" });
      setMessage("Library added. Drivebound is indexing it in the background.");
      await refresh();
      onLibraryChanged();
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : "Could not add library");
    } finally {
      setSaving(false);
    }
  };

  const scan = async (library: ExternalLibrary) => {
    await apiFetch(`/api/v1/libraries/${library.id}/scan`, { method: "POST" });
    await refresh();
    onLibraryChanged();
  };

  const protectAll = async () => {
    setMessage("Queueing verified protection copies…");
    const response = await apiFetch("/api/v1/assets/protection/protect-all", { method: "POST" });
    const result = await response.json().catch(() => null);
    setMessage(response.ok ? `${result.queued} files queued for protection.` : "Protection copies could not be queued.");
    await refresh();
  };

  const reindexMetadata = async () => {
    setMessage("Queueing metadata re-index…");
    const response = await apiFetch("/api/v1/assets/metadata/reindex", { method: "POST" });
    const result = await response.json().catch(() => null);
    setMessage(response.ok ? `${result.queued} files queued. Dates, GPS, and camera data will refresh in the background.` : "Metadata re-index could not be queued.");
  };

  const runHealthCheck = async () => { await apiFetch("/api/v1/monitoring/run", { method: "POST" }); setMessage("A full storage health check is running."); };
  const createPairingCode = async () => { const response = await apiFetch("/api/v1/nodes/pairing-code", { method: "POST" }); if (response.ok) setPairingCode((await response.json()).code); };
  const reindexIntelligence = async () => { const response = await apiFetch("/api/v1/intelligence/reindex", { method: "POST" }); const result = await response.json().catch(() => null); setMessage(response.ok ? `${result.queued} files queued for OCR and semantic search.` : "Search indexing could not be queued."); };
  const browseNode = async (node: PairedNode, path = "") => {
    const response = await apiFetch(`/api/v1/nodes/${node.id}/browse?path=${encodeURIComponent(path)}`);
    if (!response.ok) {
      setMessage(`Could not browse ${node.name}.`);
      return;
    }
    const listing = await response.json();
    setNodeBrowse((current) => ({ ...current, [node.id]: listing }));
  };

  return (
    <div className="storage-scrim" role="dialog" aria-modal="true" aria-labelledby="storage-title">
      <aside className="storage-panel">
        <div className="storage-heading">
          <div><span className="eyebrow">Capacity you control</span><h2 id="storage-title">Storage</h2></div>
          <button className="icon-button dark" onClick={onClose} aria-label="Close storage panel">×</button>
        </div>

        <section className="storage-section">
          <div className="section-title"><h3>Connected storage</h3><span>{storage?.status ?? "checking"}</span></div>
          <div className="drive-list">
            {storage?.drives.map((drive) => (
              <article className="drive-card" key={`${drive.role}-${drive.path}`}>
                <div className="drive-card-top">
                  <span className={`drive-light ${drive.status}`} />
                  <div><strong>{drive.name}</strong><small>{drive.role} · {drive.status}</small></div>
                  <b>{drive.free_bytes != null ? formatBytes(drive.free_bytes) : "—"}<small> free</small></b>
                </div>
                {drive.used_percent != null && <div className="capacity-track"><span style={{ width: `${drive.used_percent}%` }} /></div>}
                <p>{drive.path}</p>
              </article>
            ))}
          </div>
          {storage && <p className="health-note">{storage.health_note}</p>}
        </section>

        <section className="storage-section">
          <div className="section-title"><h3>Protection copies</h3><span>{protection ? `${protection.protected}/${protection.total}` : "checking"}</span></div>
          <article className="protection-card">
            <div><strong>{protection?.protected ?? 0} verified</strong><span>{protection?.queued ?? 0} in progress · {protection?.unprotected ?? 0} waiting · {protection?.failed ?? 0} need attention</span></div>
            <div className="protection-actions"><button onClick={() => void protectAll()} disabled={!protection || protection.total === 0}>Verify all</button><button onClick={() => void runHealthCheck()}>Health check</button><button onClick={() => void reindexMetadata()}>Re-index metadata</button><button onClick={() => void reindexIntelligence()}>Index text & meaning</button></div>
          </article>
          <p className="health-note">New files are protected automatically. Every copy is checksum verified and includes a metadata sidecar for disaster recovery.</p>
          {monitoring && <p className="health-note">{monitoring.open_events ? `${monitoring.open_events} issue(s) are being monitored or repaired.` : "Automated recovery is watching your originals and protection copies."}</p>}
        </section>

        <section className="storage-section">
          <div className="section-title"><h3>Paired storage nodes</h3><span>{nodes.length}</span></div>
          {nodes.length === 0 ? <p className="health-note">No external storage nodes are paired yet. Generate a code below and run the Drivebound node service on the target desktop.</p> : null}
          {nodes.map((node) => {
            const browse = nodeBrowse[node.id];
            return (
              <article className="library-card" key={node.id}>
                <div>
                  <strong>{node.name}</strong>
                  <span>{node.status}</span>
                  <small>{node.last_seen_at ? `Last seen ${new Date(node.last_seen_at).toLocaleString()}` : "Waiting for first heartbeat"}</small>
                </div>
                {node.capabilities?.mounts?.length ? (
                  <div className="storage-node-mounts">
                    {node.capabilities.mounts.map((mount) => (
                      <div key={`${node.id}-${mount.root}`} className="storage-node-mount">
                        <div className="storage-node-mount-header">
                          <strong>{mount.root}</strong>
                          <span>{mount.available ? `${formatBytes(mount.free_bytes)} free` : "Unavailable"}</span>
                        </div>
                        {mount.inventory && mount.inventory.available ? (
                          <ul className="storage-node-items">
                            {mount.inventory.items?.slice(0, 5).map((item) => (
                              <li key={`${mount.root}-${item.path ?? item.name}`}>
                                <span className={item.type === "directory" ? "item-directory" : "item-file"}>{item.type === "directory" ? "▣" : "▤"}</span>
                                <span>{item.name}</span>
                                {item.children_count != null && item.children_count > 0 ? <small>{item.children_count} items</small> : null}
                              </li>
                            ))}
                            {mount.inventory.truncated ? <li className="storage-node-more">… more items available</li> : null}
                          </ul>
                        ) : (
                          <small>{mount.reason ?? mount.inventory?.error ?? "No readable storage inventory yet."}</small>
                        )}
                      </div>
                    ))}
                  </div>
                ) : (
                  <small>No storage roots reported yet. The node will publish detected drives after it connects.</small>
                )}
                {node.endpoint_url ? (
                  <div className="storage-node-browse">
                    <button onClick={() => void browseNode(node)}>Browse storage</button>
                    {browse && browse.is_dir && browse.items && (
                      <div className="storage-node-browse-list">
                        <div className="storage-node-browse-path"><strong>{browse.path}</strong></div>
                        <ul>
                          {browse.items.slice(0, 10).map((item) => (
                            <li key={item.path}>
                              <button type="button" onClick={() => item.type === "directory" ? void browseNode(node, item.path) : undefined}>
                                {item.type === "directory" ? "▣" : "▤"} {item.name}
                              </button>
                            </li>
                          ))}
                        </ul>
                      </div>
                    )}
                  </div>
                ) : (
                  <small>Run the node service with an advertised endpoint URL to expose the storage browser.</small>
                )}
              </article>
            );
          })}
          <div className="library-form">
            <button onClick={() => void createPairingCode()}>Generate pairing code</button>
            {pairingCode ? (
              <p>
                Enter code <code>{pairingCode}</code> on the storage node within 10 minutes. Then run the node service on the target computer and it will appear here automatically.
              </p>
            ) : (
              <p>Run the Drivebound node service on the host desktop, then enter the generated value to pair that drive.</p>
            )}
          </div>
        </section>

        <section className="storage-section">
          <div className="section-title"><h3>Read-only libraries</h3><span>{libraries.length}</span></div>
          {libraries.map((library) => (
            <article className="library-card" key={library.id}>
              <div><strong>{library.name}</strong><span>{library.file_count} files · {library.status}</span><small>{library.path}</small></div>
              <button onClick={() => void scan(library)} disabled={library.status === "scanning" || library.status === "queued"}>Scan</button>
              {library.error && <p>{library.error}</p>}
            </article>
          ))}
          <div className="library-form">
            <label>Name<input value={name} onChange={(event) => setName(event.target.value)} /></label>
            <label>Container path<DirectoryPathField value={path} onChange={setPath} /></label>
            <button onClick={() => void addLibrary()} disabled={saving || !name.trim() || !path.trim()}>{saving ? "Adding…" : "Add and scan"}</button>
            <p>Choose a folder from your machine, or type a mounted path like <code>/data/imports</code> if you already know it.</p>
          </div>
          {message && <p className="storage-message" aria-live="polite">{message}</p>}
        </section>
      </aside>
    </div>
  );
}

export function PhotoLibrary({ user }: { user: DriveboundUser }) {
  const [view, setView] = useState<LibraryView>("photos");
  const [assets, setAssets] = useState<TimelineAsset[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(true);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [columns, setColumns] = useState(4);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [droppedFiles, setDroppedFiles] = useState<File[]>([]);
  const [pageDragging, setPageDragging] = useState(false);
  const [storageOpen, setStorageOpen] = useState(false);
  const [storageSummary, setStorageSummary] = useState<StorageDrive | null>(null);
  const [selected, setSelected] = useState<TimelineAsset | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const fetchingRef = useRef(false);
  const dragDepthRef = useRef(0);

  const loadPage = useCallback(async (reset = false) => {
    if (fetchingRef.current || (!reset && !hasMore)) return;
    fetchingRef.current = true;
    setLoading(true);
    setError(null);
    try {
      const cursor = reset ? null : nextCursor;
      const params = new URLSearchParams({ limit: "100" });
      if (cursor) params.set("cursor", cursor);
      const response = await apiFetch(`/api/v1/assets?${params}`);
      if (!response.ok) throw new Error(response.status === 404 ? "Create the development user before opening the library." : "The library couldn’t be reached.");
      const page: TimelineResponse = await response.json();
      setAssets((current) => reset ? page.items : [...current, ...page.items.filter((item) => !current.some((old) => old.id === item.id))]);
      setNextCursor(page.next_cursor);
      setHasMore(page.has_more);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The library couldn’t be reached.");
    } finally {
      setLoading(false);
      fetchingRef.current = false;
    }
  }, [hasMore, nextCursor]);

  useEffect(() => { void loadPage(true); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if ("serviceWorker" in navigator) void navigator.serviceWorker.register("/sw.js");
  }, []);

  useEffect(() => {
    apiFetch(`/api/v1/storage`)
      .then((response) => response.ok ? response.json() : Promise.reject())
      .then((value: StorageStatus) => setStorageSummary(value.drives.find((drive) => drive.role === "managed") ?? null))
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    const updateColumns = () => {
      const width = scrollRef.current?.clientWidth ?? window.innerWidth;
      setColumns(width < 560 ? 2 : width < 860 ? 3 : width < 1220 ? 4 : width < 1600 ? 5 : 6);
    };
    updateColumns();
    const observer = new ResizeObserver(updateColumns);
    if (scrollRef.current) observer.observe(scrollRef.current);
    return () => observer.disconnect();
  }, []);

  const rows = useMemo<TimelineRow[]>(() => {
    const grouped = new Map<string, TimelineAsset[]>();
    for (const asset of assets) grouped.set(asset.day, [...(grouped.get(asset.day) ?? []), asset]);
    const result: TimelineRow[] = [];
    for (const [day, dayAssets] of grouped) {
      result.push({ kind: "heading", day, id: `heading-${day}` });
      for (let index = 0; index < dayAssets.length; index += columns) {
        result.push({ kind: "photos", assets: dayAssets.slice(index, index + columns), id: `row-${day}-${index}` });
      }
    }
    return result;
  }, [assets, columns]);

  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: (index) => rows[index]?.kind === "heading" ? 76 : 220,
    overscan: 5,
    getItemKey: (index) => rows[index]?.id ?? index,
  });

  const virtualItems = virtualizer.getVirtualItems();
  useEffect(() => {
    const last = virtualItems.at(-1);
    if (last && last.index >= rows.length - 3 && hasMore && !loading) void loadPage();
  }, [hasMore, loadPage, loading, rows.length, virtualItems]);

  return (
    <main
      className="library-shell"
      onDragEnter={(event) => {
        if (!event.dataTransfer.types.includes("Files")) return;
        event.preventDefault();
        dragDepthRef.current += 1;
        setPageDragging(true);
      }}
      onDragOver={(event) => {
        if (!event.dataTransfer.types.includes("Files")) return;
        event.preventDefault();
        event.dataTransfer.dropEffect = "copy";
      }}
      onDragLeave={(event) => {
        if (!event.dataTransfer.types.includes("Files")) return;
        event.preventDefault();
        dragDepthRef.current = Math.max(0, dragDepthRef.current - 1);
        if (dragDepthRef.current === 0) setPageDragging(false);
      }}
      onDrop={(event) => {
        if (!event.dataTransfer.types.includes("Files")) return;
        event.preventDefault();
        dragDepthRef.current = 0;
        setPageDragging(false);
        const files = Array.from(event.dataTransfer.files);
        if (!files.length) return;
        setDroppedFiles(files);
        setUploadOpen(true);
      }}
    >
      {pageDragging && !uploadOpen && <div className="library-drop-overlay" aria-hidden="true"><span className="upload-mark">↑</span><strong>Drop to add to Drivebound</strong></div>}
      <header className="topbar">
        <div className="brand"><span className="brand-mark" aria-hidden="true">D</span><span>Drivebound</span></div>
        <nav className="library-nav" aria-label="Library sections">
          {(["photos", "memories", "files", "albums", "search", "map"] as LibraryView[]).map((item) => (
            <button key={item} className={view === item ? "active" : ""} onClick={() => setView(item)}>{item}</button>
          ))}
        </nav>
        <button className="storage-summary" onClick={() => setStorageOpen(true)}>
          <span className={`drive-light ${storageSummary?.status ?? "checking"}`} />
          <span><strong>{storageSummary?.free_bytes != null ? formatBytes(storageSummary.free_bytes) : "Storage"}</strong><small>{storageSummary ? "available" : "checking"}</small></span>
        </button>
        <div className="header-actions">
          <button className="storage-button" onClick={() => setStorageOpen(true)}>Storage</button>
          <button
            className="account-button"
            onClick={() => window.location.assign("/profile")}
            aria-label="Open profile"
          >
            <span>{(user.display_name ?? user.email).slice(0, 1).toUpperCase()}</span>
            <small>{user.display_name ?? user.email}</small>
          </button>
          <button className="upload-button" onClick={() => setUploadOpen(true)}><span aria-hidden="true">＋</span> Add files</button>
        </div>
      </header>

      {view === "photos" ? <div className="timeline-scroll" ref={scrollRef}>
        {error && !assets.length ? (
          <section className="empty-state error-state"><span className="empty-mark">!</span><h1>Library unavailable</h1><p>{error}</p><button onClick={() => void loadPage(true)}>Try again</button></section>
        ) : !loading && !assets.length ? (
          <section className="empty-state"><span className="empty-mark">✦</span><h1>Your cloud grows with your drives.</h1><p>Connect storage you own, import existing folders without moving them, or add new files with resumable transfers.</p><button onClick={() => setUploadOpen(true)}>Choose files</button></section>
        ) : (
          <div className="timeline-canvas" style={{ height: virtualizer.getTotalSize() }}>
            {virtualItems.map((virtualRow) => {
              const row = rows[virtualRow.index];
              return (
                <div
                  key={virtualRow.key}
                  ref={virtualizer.measureElement}
                  data-index={virtualRow.index}
                  className={`timeline-row ${row.kind}`}
                  style={{ transform: `translateY(${virtualRow.start}px)` }}
                >
                  {row.kind === "heading" ? (
                    <div className="date-heading"><h2>{formatDay(row.day)}</h2><span>{row.day}</span></div>
                  ) : (
                    <div className="photo-grid" style={{ gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))` }}>
                      {row.assets.map((asset) => (
                        <div key={asset.id}>
                          <PhotoTile asset={asset} onOpen={() => setSelected(asset)} />
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
        {loading && <div className="loading-line"><span /> Loading your moments</div>}
        {error && assets.length > 0 && <button className="inline-error" onClick={() => void loadPage()}>Couldn’t load more — retry</button>}
      </div> : <div className="discovery-scroll"><LibraryScreen view={view} /></div>}

      {uploadOpen && <UploadPanel initialFiles={droppedFiles} onClose={() => { setUploadOpen(false); setDroppedFiles([]); }} onFinished={() => void loadPage(true)} />}
      {storageOpen && <StoragePanel onClose={() => setStorageOpen(false)} onLibraryChanged={() => void loadPage(true)} />}
      {selected && <Viewer asset={selected} assets={assets} onClose={() => setSelected(null)} onSelect={setSelected} />}
    </main>
  );
}
