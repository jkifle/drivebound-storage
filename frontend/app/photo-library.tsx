"use client";

import { useVirtualizer } from "@tanstack/react-virtual";
import {
  useCallback,
  useEffect,
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
  logical_id?: string;
  version?: number;
  lifecycle_state?: string;
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

type AssetRevision = {
  id: string;
  logical_id: string;
  version: number;
  lifecycle_state: string;
  original_filename: string | null;
  checksum: string;
  file_size: number;
  created_at: string;
  superseded_at: string | null;
  trashed_at: string | null;
  purge_after: string | null;
};

type LifecyclePolicy = { desired_replica_count: number; retention_days: number; balance_threshold_percent: number; backup_retention_days: number };
type ReplicaDrive = { id: string; name: string; path: string; priority: number; eligible: boolean; failure_count: number; verification_status: string; last_error: string | null };
type BackupArchive = { id: string; kind: string; path: string; size_bytes: number; status: string; verification_status: string; verified_at: string | null; verification_detail: string | null; created_at: string };
type MediaGroup = { id: string; kind: string; confidence: number; members: { asset_id: string; role: string; similarity: number | null }[] };

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
type MonitoringEvent = {
  id: string;
  asset_id: string | null;
  kind: string;
  severity: string;
  status: string;
  message: string;
  detail: Record<string, unknown> | null;
  resolved_at: string | null;
  created_at: string;
};
type MonitoringOverview = { open_events: number; failed_assets: number; unprotected_assets: number; events: MonitoringEvent[] };
type PairedNode = { id: string; name: string; status: string; last_seen_at: string | null };
type BackupDevice = {
  id: string;
  name: string;
  platform: string;
  last_seen_at: string | null;
  last_backup_at: string | null;
  files_backed_up: number;
  bytes_backed_up: number;
  created_at: string;
};
type SyncRoot = { id: string; name: string; client_id: string; cursor: number; status: string; last_seen_at: string | null; created_at: string };
type SyncConflict = {
  id: string;
  root_id: string;
  root_name: string;
  logical_id: string;
  kind: string;
  detail: Record<string, unknown> | null;
  status: string;
  created_at: string;
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
  if (bytes == null) return "—";
  if (bytes === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / 1024 ** index).toFixed(index ? 1 : 0)} ${units[index]}`;
}

function formatDate(value: string | null | undefined, fallback = "Not available") {
  if (!value) return fallback;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? fallback : date.toLocaleString();
}

function humanize(value: string) {
  return value.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function retentionLabel(value: string | null, now: number) {
  if (!value) return "Retention date pending";
  const deadline = new Date(value);
  const remaining = deadline.getTime() - now;
  if (remaining <= 0) return "Retention complete — eligible for permanent deletion";
  const days = Math.ceil(remaining / 86_400_000);
  return `${days} day${days === 1 ? "" : "s"} remaining · until ${deadline.toLocaleDateString()}`;
}

const focusableElements = "a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]:not([tabindex='-1'])";

function useModalDialog(onClose: () => void, closeDisabled = false) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef(onClose);
  const closeDisabledRef = useRef(closeDisabled);

  useEffect(() => {
    closeRef.current = onClose;
    closeDisabledRef.current = closeDisabled;
  }, [closeDisabled, onClose]);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const frame = window.requestAnimationFrame(() => {
      (dialog.querySelector<HTMLElement>(focusableElements) ?? dialog).focus();
    });
    const handleKey = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape" && !closeDisabledRef.current) {
        event.preventDefault();
        closeRef.current();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = Array.from(dialog.querySelectorAll<HTMLElement>(focusableElements)).filter((element) => element.getClientRects().length > 0);
      if (!focusable.length) { event.preventDefault(); dialog.focus(); return; }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    dialog.addEventListener("keydown", handleKey);
    return () => {
      window.cancelAnimationFrame(frame);
      dialog.removeEventListener("keydown", handleKey);
      document.body.style.overflow = previousOverflow;
      previousFocus?.focus();
    };
  }, []);

  return dialogRef;
}

function PhotoTile({ asset, onOpen }: { asset: TimelineAsset; onOpen: () => void }) {
  const [failed, setFailed] = useState(false);
  const ready = asset.processing_status === "complete" && asset.thumbnail_url && !failed;
  return (
    <button className="photo-tile" onClick={onOpen} aria-label={`Open ${asset.original_filename ?? (asset.mime_type.startsWith("video/") ? "video" : "photo")} from ${formatDay(asset.day)}`}>
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
  onChanged,
}: {
  asset: TimelineAsset;
  assets: TimelineAsset[];
  onClose: () => void;
  onSelect: (asset: TimelineAsset) => void;
  onChanged: () => void;
}) {
  const [detail, setDetail] = useState<AssetDetail | null>(null);
  const [actionMessage, setActionMessage] = useState<string | null>(null);
  const [shareUrl, setShareUrl] = useState<string | null>(null);
  const [revisions, setRevisions] = useState<AssetRevision[]>([]);
  const [groups, setGroups] = useState<MediaGroup[]>([]);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [lifecycleLoading, setLifecycleLoading] = useState(true);
  const [lifecycleError, setLifecycleError] = useState<string | null>(null);
  const [confirmTrash, setConfirmTrash] = useState(false);
  const [confirmRevision, setConfirmRevision] = useState<string | null>(null);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const dialogRef = useModalDialog(onClose);
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
    let cancelled = false;
    apiFetch(`/api/v1/assets/${asset.id}/versions`)
      .then(async (response) => {
        if (!response.ok) throw new Error("Version history is unavailable.");
        return response.json();
      })
      .then((items: AssetRevision[]) => {
        if (!cancelled) setRevisions(items);
      })
      .catch((reason: unknown) => {
        if (!cancelled) setLifecycleError(reason instanceof Error ? reason.message : "Version history is unavailable.");
      })
      .finally(() => {
        if (!cancelled) setLifecycleLoading(false);
      });
    apiFetch(`/api/v1/media-groups?asset_id=${asset.id}`)
      .then((response) => response.ok ? response.json() : [])
      .then((items: MediaGroup[]) => { if (!cancelled) setGroups(items); })
      .catch(() => { if (!cancelled) setGroups([]); });
    return () => { cancelled = true; };
  }, [asset.id]);

  useEffect(() => {
    const handleKey = (event: globalThis.KeyboardEvent) => {
      if (event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement || event.target instanceof HTMLSelectElement) return;
      if (event.key === "ArrowLeft") { event.preventDefault(); move(-1); }
      if (event.key === "ArrowRight") { event.preventDefault(); move(1); }
    };
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [move]);

  const protect = async () => {
    setBusyAction("protect");
    setActionMessage("Creating a verified copy…");
    try {
      const response = await apiFetch(`/api/v1/assets/${asset.id}/protect`, { method: "POST" });
      setActionMessage(response.ok ? "Protection copy queued." : "Could not queue protection.");
    } finally {
      setBusyAction(null);
    }
  };

  const restore = async () => {
    setBusyAction("restore");
    setActionMessage("Restoring the original from its verified copy…");
    try {
      const response = await apiFetch(`/api/v1/assets/${asset.id}/restore`, { method: "POST" });
      const result = await response.json().catch(() => null);
      setActionMessage(response.ok ? "Restore queued. The original timestamp will be reapplied." : (result?.detail ?? "Restore could not be queued."));
    } finally {
      setBusyAction(null);
    }
  };

  const share = async () => {
    setBusyAction("share");
    setActionMessage("Creating private link…");
    try {
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
    } finally {
      setBusyAction(null);
    }
  };

  const trash = async () => {
    if (!confirmTrash) {
      setConfirmTrash(true);
      setActionMessage("This removes the active item from your library, but it remains recoverable during retention.");
      return;
    }
    setBusyAction("trash");
    setActionMessage("Moving this item to trash…");
    try {
      const response = await apiFetch(`/api/v1/assets/${asset.id}/trash`, { method: "POST" });
      const result = await response.json().catch(() => null);
      if (!response.ok) { setActionMessage(result?.detail ?? "Could not move this item to trash."); return; }
      onChanged();
      onClose();
    } finally {
      setBusyAction(null);
    }
  };

  const rollback = async (revision: AssetRevision) => {
    if (confirmRevision !== revision.id) {
      setConfirmRevision(revision.id);
      setActionMessage(`Restore version ${revision.version} as the active copy? Newer bytes remain in history.`);
      return;
    }
    setBusyAction(`rollback-${revision.id}`);
    setActionMessage(`Restoring version ${revision.version}…`);
    try {
      const response = await apiFetch(`/api/v1/assets/${asset.id}/rollback/${revision.id}`, { method: "POST" });
      const result = await response.json().catch(() => null);
      if (!response.ok) { setActionMessage(result?.detail ?? "Could not restore that version."); return; }
      onChanged();
      onClose();
    } finally {
      setBusyAction(null);
    }
  };

  return (
    <div className="viewer" ref={dialogRef} role="dialog" aria-modal="true" aria-labelledby="viewer-title" tabIndex={-1}>
      <div className="viewer-bar">
        <button className="icon-button" onClick={onClose} aria-label="Close viewer">×</button>
        <div className="viewer-title">
          <strong id="viewer-title">{asset.original_filename ?? formatDay(asset.day)}</strong>
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
          <video src={apiUrl(asset.original_url)} controls preload="metadata" aria-label={asset.original_filename ?? `Video from ${formatDay(asset.day)}`} />
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
          <div><dt>Lifecycle</dt><dd>Version {detail?.version ?? revisions.find((item) => item.lifecycle_state === "active")?.version ?? 1} · {humanize(detail?.lifecycle_state ?? "active")}</dd></div>
          {detail?.latitude != null && detail?.longitude != null && (
            <div><dt>Location</dt><dd>{detail.latitude.toFixed(4)}, {detail.longitude.toFixed(4)}</dd></div>
          )}
        </dl>
        <div className="asset-actions">
          <button onClick={() => void protect()} disabled={asset.protection_status === "protected" || busyAction !== null}>
            {busyAction === "protect" ? "Queueing protection…" : asset.protection_status === "protected" ? "Protected" : "Protect on backup drive"}
          </button>
          <button onClick={() => void restore()} disabled={busyAction !== null || asset.protection_status !== "protected" || asset.restore_status === "restoring" || asset.restore_status === "queued"}>
            {busyAction === "restore" || asset.restore_status === "restoring" || asset.restore_status === "queued" ? "Restore in progress" : "Restore from protected copy"}
          </button>
          <button className={confirmTrash ? "confirm-danger" : ""} onClick={() => void trash()} disabled={busyAction !== null}>
            {busyAction === "trash" ? "Moving to trash…" : confirmTrash ? "Confirm move to trash" : "Move to trash"}
          </button>
          <button onClick={() => setHistoryOpen((value) => !value)} disabled={lifecycleLoading} aria-expanded={historyOpen} aria-controls="asset-version-history">
            {lifecycleLoading ? "Loading version history…" : historyOpen ? "Hide version history" : `Version history (${revisions.length})`}
          </button>
          <button onClick={() => void share()} disabled={busyAction !== null}>{busyAction === "share" ? "Creating link…" : "Copy private link"}</button>
          {shareUrl && <input className="share-url" aria-label="Private share link" readOnly value={shareUrl} onFocus={(event) => event.currentTarget.select()} />}
          {actionMessage && <p role="status">{actionMessage}</p>}
          {lifecycleError && <p className="action-error" role="alert">{lifecycleError}</p>}
        </div>
        {historyOpen && <section className="version-history" id="asset-version-history" aria-label="Asset version history">
          <div className="history-heading"><strong>Immutable history</strong><span>{revisions.length} revision{revisions.length === 1 ? "" : "s"}</span></div>
          <p>Restoring an earlier version changes which revision is active. Every newer revision remains available here.</p>
          {revisions.map((revision) => <article key={revision.id} className={`version-row state-${revision.lifecycle_state}`}>
            <div><strong>Version {revision.version}</strong><span className={`lifecycle-pill ${revision.lifecycle_state}`}>{humanize(revision.lifecycle_state)}</span></div>
            <small>{formatDate(revision.created_at)} · {formatBytes(revision.file_size)}</small>
            <code title={revision.checksum}>{revision.checksum.slice(0, 12)}…</code>
            {revision.lifecycle_state !== "active" && revision.lifecycle_state !== "trashed" && <button onClick={() => void rollback(revision)} disabled={busyAction !== null}>
              {busyAction === `rollback-${revision.id}` ? "Restoring…" : confirmRevision === revision.id ? `Confirm version ${revision.version}` : "Restore this version"}
            </button>}
          </article>)}
        </section>}
        {groups.length > 0 && <section className="related-media" aria-label="Related media groups"><strong>Related media</strong>{groups.map((group) => <div key={group.id} className="related-row"><span>{humanize(group.kind)}</span><small>{group.members.length} items · {Math.round(group.confidence * 100)}% confidence</small></div>)}</section>}
      </aside>
    </div>
  );
}

function UploadPanel({ initialFiles = [], onClose, onFinished }: { initialFiles?: File[]; onClose: () => void; onFinished: () => void }) {
  const [uploads, setUploads] = useState<UploadItem[]>([]);
  const [dragging, setDragging] = useState(false);
  const initialFilesStarted = useRef(false);
  const active = uploads.some((item) => item.state === "waiting" || item.state === "uploading");
  const dialogRef = useModalDialog(onClose, active);

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
    <div className="upload-scrim" ref={dialogRef} role="dialog" aria-modal="true" aria-labelledby="upload-title" aria-busy={active} tabIndex={-1}>
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
          <input type="file" multiple onChange={chooseFiles} aria-describedby="upload-help" />
          <span className="upload-mark" aria-hidden="true">↑</span>
          <strong>Drop files here</strong>
          <span id="upload-help">Transfers resume in chunks and originals are checksum verified</span>
        </label>
        {uploads.length > 0 && (
          <div className="upload-list" aria-live="polite">
            {uploads.map((item) => (
              <div className="upload-row" key={item.id}>
                <div className="upload-file"><strong>{item.name}</strong><span>{item.error ?? item.state}</span></div>
                <div className="progress-track" role="progressbar" aria-label={`Upload progress for ${item.name}`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={item.progress} aria-valuetext={`${item.progress}% ${item.state}`}><span style={{ width: `${item.progress}%` }} /></div>
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

function TrashPanel({ onClose, onChanged }: { onClose: () => void; onChanged: () => void }) {
  const [items, setItems] = useState<AssetRevision[]>([]);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<Record<string, "restore" | "purge">>({});
  const [confirmPurge, setConfirmPurge] = useState<string | null>(null);
  const [queuedForPurge, setQueuedForPurge] = useState<Set<string>>(() => new Set());
  const [openedAt] = useState(() => Date.now());
  const dialogRef = useModalDialog(onClose);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await apiFetch("/api/v1/assets/trash");
      const result = await response.json().catch(() => null);
      if (!response.ok) throw new Error(result?.detail ?? "Trash could not be loaded.");
      setItems(result);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Trash could not be loaded.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const initial = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(initial);
  }, [load]);

  const restore = async (item: AssetRevision) => {
    setBusy((current) => ({ ...current, [item.id]: "restore" }));
    setError(null);
    try {
      const response = await apiFetch(`/api/v1/assets/${item.id}/restore-from-trash`, { method: "POST" });
      const result = await response.json().catch(() => null);
      if (!response.ok) { setError(result?.detail ?? "Could not restore this item."); return; }
      setItems((current) => current.filter((candidate) => candidate.id !== item.id));
      setMessage(`${item.original_filename ?? "Item"} was restored to your library.`);
      onChanged();
    } finally {
      setBusy((current) => {
        const next = { ...current };
        delete next[item.id];
        return next;
      });
    }
  };

  const purge = async (item: AssetRevision) => {
    if (confirmPurge !== item.id) {
      setConfirmPurge(item.id);
      setMessage("Permanent deletion removes the managed original, derivatives, and verified copies. This cannot be undone.");
      return;
    }
    setBusy((current) => ({ ...current, [item.id]: "purge" }));
    setError(null);
    try {
      const response = await apiFetch(`/api/v1/assets/${item.id}/purge`, { method: "POST" });
      const result = await response.json().catch(() => null);
      if (!response.ok) { setError(result?.detail ?? "Permanent deletion could not be queued."); return; }
      setQueuedForPurge((current) => new Set(current).add(item.id));
      setConfirmPurge(null);
      setMessage(`${item.original_filename ?? "Item"} is queued for permanent deletion.`);
    } finally {
      setBusy((current) => {
        const next = { ...current };
        delete next[item.id];
        return next;
      });
    }
  };

  return <div className="storage-scrim" ref={dialogRef} role="dialog" aria-modal="true" aria-labelledby="trash-title" tabIndex={-1}>
    <aside className="storage-panel trash-panel">
      <div className="storage-heading">
        <div><span className="eyebrow">Reversible deletion</span><h2 id="trash-title">Trash</h2><p>{items.length} recoverable item{items.length === 1 ? "" : "s"}</p></div>
        <button className="icon-button dark" onClick={onClose} aria-label="Close trash">×</button>
      </div>
      <section className="storage-section trash-intro">
        <div className="trash-shield" aria-hidden="true">↶</div>
        <p>Items stay recoverable until their retention date. Drivebound never permanently deletes managed bytes early, and every final deletion is recorded in the audit trail.</p>
      </section>
      <section className="storage-section trash-list" aria-live="polite">
        {loading && <div className="panel-loading" role="status"><span aria-hidden="true" /> Loading recoverable items</div>}
        {!loading && error && <div className="panel-error"><strong>Trash is unavailable</strong><p>{error}</p><button onClick={() => void load()}>Try again</button></div>}
        {!loading && !error && items.length === 0 && <div className="panel-empty"><span aria-hidden="true">✓</span><strong>Trash is empty</strong><p>Nothing is waiting for deletion.</p></div>}
        {!loading && !error && items.map((item) => {
          const purgeable = item.purge_after != null && new Date(item.purge_after).getTime() <= openedAt;
          const itemBusy = busy[item.id];
          const purgeQueued = queuedForPurge.has(item.id);
          return <article className="trash-card" key={item.id}>
            <div className="trash-card-main">
              <div className="trash-file-mark" aria-hidden="true">{item.original_filename?.split(".").pop()?.slice(0, 3).toUpperCase() || "FILE"}</div>
              <div><strong>{item.original_filename ?? "Untitled media"}</strong><span>Version {item.version} · {formatBytes(item.file_size)}</span><small>Moved {formatDate(item.trashed_at)}</small></div>
            </div>
            <div className={`retention-status ${purgeable ? "expired" : "protected"}`}><span aria-hidden="true" />{retentionLabel(item.purge_after, openedAt)}</div>
            <div className="trash-actions">
              <button onClick={() => void restore(item)} disabled={itemBusy != null || purgeQueued}>{itemBusy === "restore" ? "Restoring…" : "Restore to library"}</button>
              {purgeable && <button className={confirmPurge === item.id ? "confirm-danger" : "danger-text-button"} onClick={() => void purge(item)} disabled={itemBusy != null || purgeQueued}>
                {purgeQueued ? "Deletion queued" : itemBusy === "purge" ? "Queueing…" : confirmPurge === item.id ? "Confirm permanent deletion" : "Delete permanently"}
              </button>}
            </div>
          </article>;
        })}
      </section>
      {message && <p className="storage-message panel-message" aria-live="polite">{message}</p>}
    </aside>
  </div>;
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
  const [backupDevices, setBackupDevices] = useState<BackupDevice[]>([]);
  const [syncRoots, setSyncRoots] = useState<SyncRoot[]>([]);
  const [syncConflicts, setSyncConflicts] = useState<SyncConflict[]>([]);
  const [resolvingConflict, setResolvingConflict] = useState<string | null>(null);
  const [nodes, setNodes] = useState<PairedNode[]>([]);
  const [pairingCode, setPairingCode] = useState<string | null>(null);
  const [policy, setPolicy] = useState<LifecyclePolicy | null>(null);
  const [replicaDrives, setReplicaDrives] = useState<ReplicaDrive[]>([]);
  const [backupArchives, setBackupArchives] = useState<BackupArchive[]>([]);
  const [driveName, setDriveName] = useState("Replica drive");
  const [drivePath, setDrivePath] = useState("/data/replicas");
  const [healthCheckRunning, setHealthCheckRunning] = useState(false);
  const [refreshError, setRefreshError] = useState<string | null>(null);
  const [lastRefreshedAt, setLastRefreshedAt] = useState<string | null>(null);
  const [actionBusy, setActionBusy] = useState<string | null>(null);
  const dialogRef = useModalDialog(onClose);

  const refresh = useCallback(async () => {
    try {
      const [storageResponse, libraryResponse, protectionResponse, monitoringResponse, nodesResponse, policyResponse, drivesResponse, backupsResponse, devicesResponse, syncRootsResponse] = await Promise.all([
        apiFetch(`/api/v1/storage`),
        apiFetch(`/api/v1/libraries`),
        apiFetch(`/api/v1/assets/protection/status`),
        apiFetch(`/api/v1/monitoring`),
        apiFetch(`/api/v1/nodes`),
        apiFetch(`/api/v1/storage/policy`),
        apiFetch(`/api/v1/storage/drives`),
        apiFetch(`/api/v1/storage/backups`),
        apiFetch(`/api/v1/devices`),
        apiFetch(`/api/v1/sync/roots`),
      ]);
      if (storageResponse.ok) setStorage(await storageResponse.json());
      if (libraryResponse.ok) setLibraries(await libraryResponse.json());
      if (protectionResponse.ok) setProtection(await protectionResponse.json());
      if (monitoringResponse.ok) setMonitoring(await monitoringResponse.json());
      if (nodesResponse.ok) setNodes(await nodesResponse.json());
      if (policyResponse.ok) setPolicy(await policyResponse.json());
      if (drivesResponse.ok) setReplicaDrives(await drivesResponse.json());
      if (backupsResponse.ok) setBackupArchives(await backupsResponse.json());
      if (devicesResponse.ok) setBackupDevices(await devicesResponse.json());
      if (syncRootsResponse.ok) {
        const roots: SyncRoot[] = await syncRootsResponse.json();
        setSyncRoots(roots);
        const conflictLists = await Promise.all(roots.map(async (root) => {
          const response = await apiFetch(`/api/v1/sync/roots/${root.id}/conflicts`);
          if (!response.ok) return [];
          const conflicts: Omit<SyncConflict, "root_id" | "root_name">[] = await response.json();
          return conflicts.map((conflict) => ({ ...conflict, root_id: root.id, root_name: root.name }));
        }));
        setSyncConflicts(conflictLists.flat());
      }
      if ([storageResponse, monitoringResponse, protectionResponse].some((response) => !response.ok)) {
        setRefreshError("Some recovery status could not be refreshed.");
      } else {
        setRefreshError(null);
      }
      setLastRefreshedAt(new Date().toISOString());
    } catch {
      setRefreshError("Drivebound could not reach the recovery services. Existing status may be stale.");
    }
  }, []);

  useEffect(() => {
    const initial = window.setTimeout(() => void refresh(), 0);
    const interval = window.setInterval(() => void refresh(), 15000);
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
    setActionBusy(`scan-${library.id}`);
    setMessage(null);
    try {
      const response = await apiFetch(`/api/v1/libraries/${library.id}/scan`, { method: "POST" });
      setMessage(response.ok ? `${library.name} is queued for scanning.` : `${library.name} could not be scanned.`);
      if (response.ok) { await refresh(); onLibraryChanged(); }
    } catch {
      setMessage(`${library.name} could not be scanned.`);
    } finally {
      setActionBusy(null);
    }
  };

  const protectAll = async () => {
    setActionBusy("protect-all");
    setMessage("Queueing verified protection copies…");
    try {
      const response = await apiFetch("/api/v1/assets/protection/protect-all", { method: "POST" });
      const result = await response.json().catch(() => null);
      setMessage(response.ok ? `${result.queued} files queued for protection.` : (result?.detail ?? "Protection copies could not be queued."));
      if (response.ok) await refresh();
    } catch {
      setMessage("Protection copies could not be queued.");
    } finally {
      setActionBusy(null);
    }
  };

  const reindexMetadata = async () => {
    setActionBusy("metadata");
    setMessage("Queueing metadata re-index…");
    try {
      const response = await apiFetch("/api/v1/assets/metadata/reindex", { method: "POST" });
      const result = await response.json().catch(() => null);
      setMessage(response.ok ? `${result.queued} files queued. Dates, GPS, and camera data will refresh in the background.` : (result?.detail ?? "Metadata re-index could not be queued."));
    } catch {
      setMessage("Metadata re-index could not be queued.");
    } finally {
      setActionBusy(null);
    }
  };

  const runHealthCheck = async () => {
    setHealthCheckRunning(true);
    const response = await apiFetch("/api/v1/monitoring/run", { method: "POST" }).catch(() => null);
    if (!response?.ok) {
      setMessage("The health check could not be started.");
      setHealthCheckRunning(false);
      return;
    }
    setMessage("A full storage health check is running. Results refresh automatically.");
    window.setTimeout(() => { void refresh(); setHealthCheckRunning(false); }, 1800);
  };
  const createPairingCode = async () => {
    setActionBusy("pair");
    try {
      const response = await apiFetch("/api/v1/nodes/pairing-code", { method: "POST" });
      const result = await response.json().catch(() => null);
      if (response.ok) { setPairingCode(result.code); setMessage("Pairing code created. It expires in 10 minutes."); }
      else setMessage(result?.detail ?? "A pairing code could not be created.");
    } catch { setMessage("A pairing code could not be created."); }
    finally { setActionBusy(null); }
  };
  const reindexIntelligence = async () => {
    setActionBusy("intelligence");
    try {
      const response = await apiFetch("/api/v1/intelligence/reindex", { method: "POST" });
      const result = await response.json().catch(() => null);
      setMessage(response.ok ? `${result.queued} files queued for OCR and semantic search.` : (result?.detail ?? "Search indexing could not be queued."));
    } catch { setMessage("Search indexing could not be queued."); }
    finally { setActionBusy(null); }
  };
  const savePolicy = async () => {
    if (!policy) return;
    setActionBusy("policy");
    try {
      const response = await apiFetch("/api/v1/storage/policy", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(policy) });
      const result = await response.json().catch(() => null);
      setMessage(response.ok ? "Storage policy saved. New copies will follow it." : (result?.detail ?? "Could not save the storage policy."));
      if (response.ok) await refresh();
    } catch { setMessage("Could not save the storage policy."); }
    finally { setActionBusy(null); }
  };
  const addReplicaDrive = async () => {
    setActionBusy("replica");
    try {
      const response = await apiFetch("/api/v1/storage/drives", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: driveName, path: drivePath }) });
      const result = await response.json().catch(() => null);
      setMessage(response.ok ? "Replica drive added. New protection jobs can use it." : (result?.detail ?? "Could not add replica drive."));
      if (response.ok) await refresh();
    } catch { setMessage("Could not add replica drive."); }
    finally { setActionBusy(null); }
  };
  const balance = async () => {
    setActionBusy("balance");
    try {
      const response = await apiFetch("/api/v1/storage/balance", { method: "POST" });
      const result = await response.json().catch(() => null);
      setMessage(response.ok ? "Storage balancing queued. Existing verified copies stay in place until replacements verify." : (result?.detail ?? "Storage balancing could not be queued."));
    } catch { setMessage("Storage balancing could not be queued."); }
    finally { setActionBusy(null); }
  };
  const resolveSyncConflict = async (conflict: SyncConflict, choice: "keep_local" | "keep_remote") => {
    setResolvingConflict(conflict.id);
    const response = await apiFetch(`/api/v1/sync/conflicts/${conflict.id}/resolve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ choice }),
    }).catch(() => null);
    if (response?.ok) {
      setSyncConflicts((current) => current.filter((item) => item.id !== conflict.id));
      setMessage(choice === "keep_local" ? "The device change is now the current sync revision." : "The Drivebound copy was kept and the device change was rejected.");
    } else {
      const result = response ? await response.json().catch(() => null) : null;
      setMessage(result?.detail ?? "That sync conflict could not be resolved.");
    }
    setResolvingConflict(null);
  };

  return (
    <div className="storage-scrim" ref={dialogRef} role="dialog" aria-modal="true" aria-labelledby="storage-title" tabIndex={-1}>
      <aside className="storage-panel">
        <div className="storage-heading">
          <div><span className="eyebrow">Capacity you control</span><h2 id="storage-title">Storage</h2><p>{lastRefreshedAt ? `Updated ${new Date(lastRefreshedAt).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" })}` : "Checking recovery systems…"}</p></div>
          <button className="icon-button dark" onClick={onClose} aria-label="Close storage panel">×</button>
        </div>
        {refreshError && <div className="panel-warning" role="status">{refreshError}</div>}
        {message && <p className="storage-message panel-message" role="status">{message}</p>}

        <section className="storage-section">
          <div className="section-title"><h3>Connected storage</h3><span>{storage?.status ?? "checking"}</span></div>
          <div className="drive-list">
            {storage?.drives.map((drive) => (
              <article className="drive-card" key={`${drive.role}-${drive.path}`}>
                <div className="drive-card-top">
                  <span className={`drive-light ${drive.status}`} aria-hidden="true" />
                  <div><strong>{drive.name}</strong><small>{drive.role} · {drive.status}</small></div>
                  <b>{drive.free_bytes != null ? formatBytes(drive.free_bytes) : "—"}<small> free</small></b>
                </div>
                {drive.used_percent != null && <div className="capacity-track" role="progressbar" aria-label={`${drive.name} capacity used`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={drive.used_percent}><span style={{ width: `${drive.used_percent}%` }} /></div>}
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
            <div className="protection-actions"><button onClick={() => void protectAll()} disabled={actionBusy != null || !protection || protection.total === 0}>{actionBusy === "protect-all" ? "Queueing…" : "Verify all"}</button><button onClick={() => void runHealthCheck()} disabled={healthCheckRunning || actionBusy != null}>{healthCheckRunning ? "Checking…" : "Health check"}</button><button onClick={() => void reindexMetadata()} disabled={actionBusy != null}>{actionBusy === "metadata" ? "Queueing…" : "Re-index metadata"}</button><button onClick={() => void reindexIntelligence()} disabled={actionBusy != null}>{actionBusy === "intelligence" ? "Queueing…" : "Index text & meaning"}</button></div>
          </article>
          <p className="health-note">New files are protected automatically. Every copy is checksum verified and includes a metadata sidecar for disaster recovery.</p>
          {monitoring && <p className="health-note">{monitoring.open_events ? `${monitoring.open_events} issue(s) are being monitored or repaired.` : "Automated recovery is watching your originals and protection copies."}</p>}
        </section>

        <section className="storage-section recovery-section">
          <div className="section-title"><h3>Recovery monitor</h3><span className={monitoring?.open_events ? "attention" : "healthy"}>{monitoring ? (monitoring.open_events ? "attention" : "healthy") : "checking"}</span></div>
          <div className="monitoring-overview" aria-label="Recovery monitoring summary">
            <article><strong>{monitoring?.open_events ?? "—"}</strong><span>Open events</span></article>
            <article><strong>{monitoring?.failed_assets ?? "—"}</strong><span>Failed processing</span></article>
            <article><strong>{monitoring?.unprotected_assets ?? "—"}</strong><span>Need protection</span></article>
          </div>
          <div className="monitoring-events">
            <div className="monitoring-events-heading"><strong>Recent activity</strong><button onClick={() => void refresh()} aria-label="Refresh recovery activity" disabled={actionBusy != null}>Refresh</button></div>
            {monitoring?.events.slice(0, 8).map((event) => <article className={`monitoring-event severity-${event.severity} status-${event.status}`} key={event.id}>
              <span className="event-light" aria-hidden="true" />
              <div><strong>{event.message}</strong><small>{humanize(event.kind)} · {formatDate(event.created_at)}</small></div>
              <span className="event-status">{event.status}</span>
            </article>)}
            {monitoring && monitoring.events.length === 0 && <div className="panel-empty compact"><span aria-hidden="true">✓</span><strong>No recovery events</strong><p>Your monitored storage is quiet.</p></div>}
          </div>
        </section>

        <section className="storage-section">
          <div className="section-title"><h3>Device backups</h3><span>{backupDevices.length}</span></div>
          <div className="device-backup-list">
            {backupDevices.map((device) => <article className="device-backup-card" key={device.id}>
              <div className="device-icon" aria-hidden="true">{device.platform.toLowerCase().includes("ios") ? "iOS" : "A"}</div>
              <div><strong>{device.name}</strong><span>{humanize(device.platform)} · {device.files_backed_up.toLocaleString()} files · {formatBytes(device.bytes_backed_up)}</span><small>{device.last_backup_at ? `Last backup ${formatDate(device.last_backup_at)}` : `Connected ${formatDate(device.created_at)} · waiting for first backup`}</small></div>
              <span className={`backup-state ${device.last_backup_at ? "complete" : "waiting"}`}>{device.last_backup_at ? "backed up" : "waiting"}</span>
            </article>)}
            {backupDevices.length === 0 && <p className="health-note">Connect the Drivebound mobile app to see automatic camera-roll backup status here.</p>}
          </div>
        </section>

        <section className="storage-section">
          <div className="section-title"><h3>Desktop sync</h3><span>{syncConflicts.length ? `${syncConflicts.length} conflict${syncConflicts.length === 1 ? "" : "s"}` : `${syncRoots.length} folder${syncRoots.length === 1 ? "" : "s"}`}</span></div>
          <div className="sync-root-list">
            {syncRoots.map((root) => <article className="sync-root-card" key={root.id}>
              <span className={`drive-light ${root.status === "active" ? "online" : root.status}`} aria-hidden="true" />
              <div><strong>{root.name}</strong><span>{humanize(root.status)} · journal position {root.cursor.toLocaleString()}</span><small>{root.last_seen_at ? `Last synchronized ${formatDate(root.last_seen_at)}` : `Registered ${formatDate(root.created_at)} · waiting for first sync`}</small></div>
            </article>)}
            {syncRoots.length === 0 && <p className="health-note">Pair the desktop sync client to keep selected folders aligned without changing files outside those roots.</p>}
          </div>
          {syncConflicts.length > 0 && <div className="sync-conflict-list" aria-label="Open desktop sync conflicts">
            <strong>Needs your decision</strong>
            <p>Both sides changed from the same earlier revision. Choose which change becomes current; the media history remains preserved.</p>
            {syncConflicts.map((conflict) => <article className="sync-conflict-card" key={conflict.id}>
              <div><strong>{humanize(conflict.kind)}</strong><span>{conflict.root_name} · {formatDate(conflict.created_at)}</span><small>Item {conflict.logical_id.slice(0, 8)} · server revision {String(conflict.detail?.actual_revision ?? "new")}</small></div>
              <div className="sync-conflict-actions"><button onClick={() => void resolveSyncConflict(conflict, "keep_remote")} disabled={resolvingConflict !== null}>Keep Drivebound copy</button><button onClick={() => void resolveSyncConflict(conflict, "keep_local")} disabled={resolvingConflict !== null}>{resolvingConflict === conflict.id ? "Resolving…" : "Use device change"}</button></div>
            </article>)}
          </div>}
        </section>

        <section className="storage-section">
          <div className="section-title"><h3>Replica policy</h3><span>{policy?.desired_replica_count ?? "—"} copies</span></div>
          {policy && <div className="policy-form">
            <label>Verified copies per item<input type="number" min="1" max="8" value={policy.desired_replica_count} onChange={(event) => setPolicy({ ...policy, desired_replica_count: Number(event.target.value) || 1 })} /></label>
            <label>Trash retention (days)<input type="number" min="1" max="3650" value={policy.retention_days} onChange={(event) => setPolicy({ ...policy, retention_days: Number(event.target.value) || 30 })} /></label>
            <label>Backup retention (days)<input type="number" min="1" max="3650" value={policy.backup_retention_days} onChange={(event) => setPolicy({ ...policy, backup_retention_days: Number(event.target.value) || 30 })} /></label>
            <label>Balance threshold (%)<input type="number" min="1" max="80" value={policy.balance_threshold_percent} onChange={(event) => setPolicy({ ...policy, balance_threshold_percent: Number(event.target.value) || 12 })} /></label>
            <div className="protection-actions"><button onClick={() => void savePolicy()} disabled={actionBusy != null}>{actionBusy === "policy" ? "Saving…" : "Save policy"}</button><button onClick={() => void balance()} disabled={actionBusy != null}>{actionBusy === "balance" ? "Queueing…" : "Balance verified copies"}</button></div>
          </div>}
          <p className="health-note">A copy is counted only after its checksum verifies. A failed drive leaves healthy copies untouched and the policy visibly degraded.</p>
          {replicaDrives.map((drive) => <article className="library-card" key={drive.id}><div><strong>{drive.name}</strong><span>{drive.verification_status} · priority {drive.priority}</span><small>{drive.path}{drive.last_error ? ` — ${drive.last_error}` : ""}</small></div></article>)}
          <div className="library-form"><label>Replica drive name<input value={driveName} onChange={(event) => setDriveName(event.target.value)} /></label><label>Approved container path<input value={drivePath} onChange={(event) => setDrivePath(event.target.value)} spellCheck={false} /></label><button onClick={() => void addReplicaDrive()} disabled={actionBusy != null || !driveName.trim() || !drivePath.trim()}>{actionBusy === "replica" ? "Adding…" : "Add replica drive"}</button></div>
        </section>

        <section className="storage-section">
          <div className="section-title"><h3>Operational backups</h3><span>{backupArchives.length}</span></div>
          {backupArchives.slice(0, 5).map((backup) => <article className="library-card" key={backup.id}><div><strong>{humanize(backup.kind)} backup</strong><span>{humanize(backup.verification_status)} · {formatBytes(backup.size_bytes)}</span><small>{backup.verified_at ? `Verified ${formatDate(backup.verified_at)}` : backup.verification_detail ?? `Created ${formatDate(backup.created_at)}`}</small></div><span className={`backup-state ${backup.verification_status === "verified" ? "complete" : "waiting"}`}>{backup.status}</span></article>)}
          {backupArchives.length === 0 && <p className="health-note">No operational backup archive has been recorded yet.</p>}
          <p className="health-note">Configuration changes are deduplicated; database archives are logically checked before they count as recoverable.</p>
        </section>

        <section className="storage-section"><div className="section-title"><h3>Paired storage nodes</h3><span>{nodes.length}</span></div>{nodes.map(node => <article className="library-card" key={node.id}><div><strong>{node.name}</strong><span>{node.status}</span><small>{node.last_seen_at ? `Last seen ${new Date(node.last_seen_at).toLocaleString()}` : "Waiting for first heartbeat"}</small></div></article>)}<div className="library-form"><button onClick={() => void createPairingCode()} disabled={actionBusy != null}>{actionBusy === "pair" ? "Creating code…" : "Pair another drive"}</button>{pairingCode && <p role="status">Enter code <code>{pairingCode}</code> on the storage node within 10 minutes.</p>}</div></section>

        <section className="storage-section">
          <div className="section-title"><h3>Read-only libraries</h3><span>{libraries.length}</span></div>
          {libraries.map((library) => (
            <article className="library-card" key={library.id}>
              <div><strong>{library.name}</strong><span>{library.file_count} files · {library.status}</span><small>{library.path}</small></div>
              <button onClick={() => void scan(library)} disabled={actionBusy != null || library.status === "scanning" || library.status === "queued"}>{actionBusy === `scan-${library.id}` ? "Queueing…" : "Scan"}</button>
              {library.error && <p>{library.error}</p>}
            </article>
          ))}
          <div className="library-form">
            <label>Name<input value={name} onChange={(event) => setName(event.target.value)} /></label>
            <label>Container path<input value={path} onChange={(event) => setPath(event.target.value)} spellCheck={false} /><small>Use a path mounted inside the Drivebound server or container.</small></label>
            <button onClick={() => void addLibrary()} disabled={saving || actionBusy != null || !name.trim() || !path.trim()}>{saving ? "Adding…" : "Add and scan"}</button>
            <p>Place existing media in <code>data/imports</code>, or mount another read-only folder at <code>/data/imports</code>.</p>
          </div>
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
  const [trashOpen, setTrashOpen] = useState(false);
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
      id="main-content"
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
            <button key={item} className={view === item ? "active" : ""} onClick={() => setView(item)} aria-current={view === item ? "page" : undefined}>{item}</button>
          ))}
        </nav>
        <button className="storage-summary" onClick={() => setStorageOpen(true)} aria-label={storageSummary?.free_bytes != null ? `Open storage status, ${formatBytes(storageSummary.free_bytes)} available` : "Open storage status, checking capacity"}>
          <span className={`drive-light ${storageSummary?.status ?? "checking"}`} aria-hidden="true" />
          <span><strong>{storageSummary?.free_bytes != null ? formatBytes(storageSummary.free_bytes) : "Storage"}</strong><small>{storageSummary ? "available" : "checking"}</small></span>
        </button>
        <div className="header-actions">
          <button className="storage-button utility-control" onClick={() => setStorageOpen(true)} aria-label="Open storage and recovery status"><span className="utility-mark" aria-hidden="true">▣</span><span className="utility-label">Storage</span></button>
          <button className="storage-button utility-control" onClick={() => setTrashOpen(true)} aria-label="Open trash"><span className="utility-mark" aria-hidden="true">↶</span><span className="utility-label">Trash</span></button>
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
      <p className="sr-only" role="status">{humanize(view)} view</p>

      {view === "photos" ? <div className="timeline-scroll" ref={scrollRef} aria-busy={loading}>
        {error && !assets.length ? (
          <section className="empty-state error-state" role="alert"><span className="empty-mark" aria-hidden="true">!</span><h1>Library unavailable</h1><p>{error}</p><button onClick={() => void loadPage(true)}>Try again</button></section>
        ) : !loading && !assets.length ? (
          <section className="empty-state"><span className="empty-mark" aria-hidden="true">✦</span><h1>Your cloud grows with your drives.</h1><p>Connect storage you own, import existing folders without moving them, or add new files with resumable transfers.</p><button onClick={() => setUploadOpen(true)}>Choose files</button></section>
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
        {loading && <div className="loading-line" role="status"><span aria-hidden="true" /> Loading your moments</div>}
        {error && assets.length > 0 && <button className="inline-error" onClick={() => void loadPage()}>Couldn’t load more — retry</button>}
      </div> : <div className="discovery-scroll"><LibraryScreen view={view} /></div>}

      {uploadOpen && <UploadPanel initialFiles={droppedFiles} onClose={() => { setUploadOpen(false); setDroppedFiles([]); }} onFinished={() => void loadPage(true)} />}
      {storageOpen && <StoragePanel onClose={() => setStorageOpen(false)} onLibraryChanged={() => void loadPage(true)} />}
      {trashOpen && <TrashPanel onClose={() => setTrashOpen(false)} onChanged={() => void loadPage(true)} />}
      {selected && <Viewer key={selected.id} asset={selected} assets={assets} onClose={() => setSelected(null)} onSelect={setSelected} onChanged={() => void loadPage(true)} />}
    </main>
  );
}
