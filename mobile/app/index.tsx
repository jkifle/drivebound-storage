import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ActivityIndicator,
  Alert,
  AppState,
  Image,
  Linking,
  Modal,
  Platform,
  SafeAreaView,
  ScrollView,
  Share,
  StyleSheet,
  Switch,
  Text,
  TextInput,
  TouchableOpacity,
  View,
} from "react-native";
import * as Network from "expo-network";
import { VideoView, useVideoPlayer } from "expo-video";

import {
  DriveboundApiError,
  type Album,
  type AssetDetail,
  type MapAsset,
  type TimelineAsset,
  albumAssets,
  albums,
  assetDetail,
  cachedMediaUri,
  clearConnection,
  createShare,
  isConnected,
  mapAssets,
  mediaUrl,
  memories,
  restoreToPhone,
  search,
  timeline,
  verifyConnection,
} from "../src/api";
import {
  backupQueueSnapshot,
  cancelPendingBackupTransfers,
  configureDevice,
  retryFailedBackupTransfers,
  runBackup,
  type BackupProgress,
} from "../src/backup";
import { setAutomaticBackup } from "../src/background";
import { configureNotifications } from "../src/notifications";
import { defaultBackupPolicy, loadBackupPolicy, saveBackupPolicy, type BackupPolicy } from "../src/policies";
import type { QueueSummary, Transfer } from "../src/queue";

type Tab = "library" | "discover" | "backup" | "settings";

const emptySummary: QueueSummary = {
  pending: 0,
  uploading: 0,
  failed: 0,
  complete: 0,
  bytesPending: 0,
  nextRetryAt: null,
};

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
  return `${(bytes / (1024 ** index)).toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

function formatDate(value: string | null): string {
  if (!value) return "Not available";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? "Not available" : date.toLocaleString();
}

function networkLabel(state: Network.NetworkState): string {
  if (!state.isConnected) return "Offline";
  if (state.type === Network.NetworkStateType.WIFI) return "Wi-Fi connected";
  if (state.type === Network.NetworkStateType.ETHERNET) return "Ethernet connected";
  if (state.type === Network.NetworkStateType.CELLULAR) return "Cellular connected";
  return "Network connected";
}

function AuthenticatedImage({ path, style }: { path: string | null; style?: object }) {
  const [source, setSource] = useState<{ uri: string } | null>(null);
  useEffect(() => {
    let active = true;
    setSource(null);
    if (path) cachedMediaUri(path).then(uri => active && setSource({ uri })).catch(() => active && setSource(null));
    return () => { active = false; };
  }, [path]);
  if (!source) return <View style={[styles.placeholder, style]}><Text style={styles.placeholderText}>IMG</Text></View>;
  return <Image source={source} style={[styles.image, style]} resizeMode="cover" />;
}

function VideoMedia({ path }: { path: string }) {
  const [source, setSource] = useState<{ uri: string; headers: Record<string, string> } | null>(null);
  useEffect(() => {
    let active = true;
    mediaUrl(path).then(value => active && setSource(value)).catch(() => active && setSource(null));
    return () => { active = false; };
  }, [path]);
  const player = useVideoPlayer(source, videoPlayer => { videoPlayer.loop = false; });
  if (!source) return <View style={styles.viewerLoading}><ActivityIndicator color="#fff" /></View>;
  return <VideoView style={styles.viewerMedia} player={player} nativeControls />;
}

function AssetCard({ asset, onPress }: { asset: TimelineAsset; onPress: (asset: TimelineAsset) => void }) {
  return <TouchableOpacity
    style={styles.assetCard}
    onPress={() => onPress(asset)}
    accessibilityRole="button"
    accessibilityLabel={`Open ${asset.original_filename || "media"}`}
  >
    <AuthenticatedImage path={asset.thumbnail_url ?? asset.original_url} style={styles.assetImage} />
    {asset.mime_type.startsWith("video/") && <View style={styles.videoBadge}><Text style={styles.videoBadgeText}>PLAY</Text></View>}
    <Text numberOfLines={1} style={styles.assetName}>{asset.original_filename || "Untitled media"}</Text>
    <Text style={styles.assetDate}>{new Date(asset.timeline_at).toLocaleDateString()}</Text>
  </TouchableOpacity>;
}

function AssetGrid({ assets, onSelect }: { assets: TimelineAsset[]; onSelect: (asset: TimelineAsset) => void }) {
  if (!assets.length) return <Text style={styles.empty}>Nothing here yet.</Text>;
  return <View style={styles.grid}>{assets.map(asset => <AssetCard key={asset.id} asset={asset} onPress={onSelect} />)}</View>;
}

function DetailRow({ label, value }: { label: string; value: string }) {
  return <View style={styles.detailRow}><Text style={styles.detailLabel}>{label}</Text><Text style={styles.detailValue}>{value}</Text></View>;
}

function Viewer({ asset, onClose }: { asset: TimelineAsset | null; onClose: () => void }) {
  const [busyAction, setBusyAction] = useState<"share" | "download" | null>(null);
  const [detail, setDetail] = useState<AssetDetail | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    setDetail(null);
    setDetailError(null);
    if (asset) {
      assetDetail(asset.id)
        .then(value => active && setDetail(value))
        .catch(error => active && setDetailError(errorMessage(error)));
    }
    return () => { active = false; };
  }, [asset]);

  if (!asset) return null;
  const shareAsset = async () => {
    setBusyAction("share");
    try {
      const result = await createShare(asset.id);
      await Share.share({ message: result.url, url: result.url, title: asset.original_filename || "Drivebound media" });
    } catch (error) {
      Alert.alert("Could not create share", errorMessage(error));
    } finally {
      setBusyAction(null);
    }
  };
  const restore = async () => {
    setBusyAction("download");
    try {
      await restoreToPhone(asset);
      Alert.alert("Saved to your phone", "The untouched original was added to your photo library.");
    } catch (error) {
      Alert.alert("Download failed", errorMessage(error));
    } finally {
      setBusyAction(null);
    }
  };
  const camera = [detail?.camera_make, detail?.camera_model].filter(Boolean).join(" ");
  const location = detail?.latitude !== null && detail?.latitude !== undefined && detail?.longitude !== null && detail?.longitude !== undefined
    ? `${detail.latitude.toFixed(5)}, ${detail.longitude.toFixed(5)}`
    : "Not available";

  return <Modal visible animationType="slide" onRequestClose={onClose}>
    <SafeAreaView style={styles.viewerSafe}>
      <View style={styles.viewerHeader}>
        <TouchableOpacity onPress={onClose} accessibilityRole="button"><Text style={styles.viewerClose}>Close</Text></TouchableOpacity>
        <Text numberOfLines={1} style={styles.viewerTitle}>{asset.original_filename || "Media"}</Text>
        <View style={styles.viewerHeaderSpacer} />
      </View>
      <View style={styles.viewerPreview}>
        {asset.mime_type.startsWith("video/")
          ? <VideoMedia path={asset.original_url} />
          : <AuthenticatedImage path={asset.original_url} style={styles.viewerMedia} />}
      </View>
      <ScrollView style={styles.detailSheet} contentContainerStyle={styles.detailContent}>
        <Text style={styles.detailHeading}>Original details</Text>
        {!detail && !detailError && <ActivityIndicator color="#2266d5" />}
        {detailError && <Text style={styles.detailError}>{detailError}</Text>}
        {detail && <>
          <DetailRow label="Captured" value={formatDate(detail.taken_at ?? detail.file_created_at)} />
          <DetailRow label="Size" value={formatBytes(detail.file_size)} />
          <DetailRow label="Dimensions" value={detail.width && detail.height ? `${detail.width} x ${detail.height}` : "Not available"} />
          <DetailRow label="Camera" value={camera || "Not available"} />
          <DetailRow label="Lens" value={detail.lens_model || "Not available"} />
          <DetailRow label="Location" value={location} />
          <DetailRow label="Protection" value={detail.protection_status} />
        </>}
      </ScrollView>
      <View style={styles.viewerActions}>
        <TouchableOpacity disabled={busyAction !== null} style={[styles.secondaryButton, styles.actionButton]} onPress={restore}>
          <Text style={styles.secondaryButtonText}>{busyAction === "download" ? "Saving..." : "Save original"}</Text>
        </TouchableOpacity>
        <TouchableOpacity disabled={busyAction !== null} style={[styles.primaryButton, styles.actionButton]} onPress={shareAsset}>
          <Text style={styles.primaryButtonText}>{busyAction === "share" ? "Creating..." : "Share"}</Text>
        </TouchableOpacity>
      </View>
    </SafeAreaView>
  </Modal>;
}

function QueueItem({ transfer }: { transfer: Transfer }) {
  const percent = transfer.fileSize > 0 ? Math.min(100, Math.round((transfer.offset / transfer.fileSize) * 100)) : 0;
  const status = transfer.state === "retry" && transfer.retryAt > Date.now()
    ? `Retry ${new Date(transfer.retryAt).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`
    : transfer.state === "uploading" ? `${percent}% uploaded` : transfer.state;
  return <View style={styles.queueItem}>
    <View style={styles.queueItemHeader}>
      <Text numberOfLines={1} style={styles.queueName}>{transfer.filename}</Text>
      <Text style={styles.queueState}>{status}</Text>
    </View>
    {transfer.fileSize > 0 && <View style={styles.progressTrack}><View style={[styles.progressFill, { width: `${percent}%` }]} /></View>}
    {transfer.lastError && <Text numberOfLines={2} style={styles.queueError}>{transfer.lastError}</Text>}
  </View>;
}

export default function Home() {
  const [connected, setConnected] = useState<boolean | null>(null);
  const [tab, setTab] = useState<Tab>("library");
  const configuredServer = (process.env.EXPO_PUBLIC_API_URL ?? "").trim().replace(/\/$/, "");
  const developmentBuild = (process.env.EXPO_PUBLIC_APP_ENV ?? "development") === "development";
  const developmentServer = Platform.OS === "android" ? "http://10.0.2.2:8000" : "http://localhost:8000";
  const [server, setServer] = useState(configuredServer || (developmentBuild ? developmentServer : ""));
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [otp, setOtp] = useState("");
  const [message, setMessage] = useState("Connect this phone to begin.");
  const [library, setLibrary] = useState<TimelineAsset[]>([]);
  const [libraryCursor, setLibraryCursor] = useState<string | null>(null);
  const [libraryHasMore, setLibraryHasMore] = useState(false);
  const [selected, setSelected] = useState<TimelineAsset | null>(null);
  const [loading, setLoading] = useState(false);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<TimelineAsset[]>([]);
  const [memoryItems, setMemoryItems] = useState<TimelineAsset[]>([]);
  const [albumItems, setAlbumItems] = useState<Album[]>([]);
  const [activeAlbum, setActiveAlbum] = useState<Album | null>(null);
  const [mapItems, setMapItems] = useState<MapAsset[]>([]);
  const [progress, setProgress] = useState<BackupProgress | null>(null);
  const [summary, setSummary] = useState<QueueSummary>(emptySummary);
  const [queueItems, setQueueItems] = useState<Transfer[]>([]);
  const [policy, setPolicy] = useState<BackupPolicy>(defaultBackupPolicy);
  const [network, setNetwork] = useState("Checking connection...");
  const reconnectRunRef = useRef(false);

  const refreshQueue = useCallback(async () => {
    try {
      const snapshot = await backupQueueSnapshot();
      setSummary(snapshot.summary);
      setQueueItems(snapshot.transfers);
    } catch { /* The connection screen handles missing credentials. */ }
  }, []);

  const refreshLibrary = useCallback(async () => {
    setLoading(true);
    try {
      const page = await timeline();
      setLibrary(page.items);
      setLibraryCursor(page.next_cursor);
      setLibraryHasMore(page.has_more);
    } catch (error) {
      setMessage(errorMessage(error));
    } finally {
      setLoading(false);
    }
  }, []);

  const loadMoreLibrary = async () => {
    if (!libraryCursor || loading) return;
    setLoading(true);
    try {
      const page = await timeline(libraryCursor);
      setLibrary(current => {
        const known = new Set(current.map(item => item.id));
        return [...current, ...page.items.filter(item => !known.has(item.id))];
      });
      setLibraryCursor(page.next_cursor);
      setLibraryHasMore(page.has_more);
    } catch (error) {
      setMessage(errorMessage(error));
    } finally {
      setLoading(false);
    }
  };

  const refreshDiscover = useCallback(async () => {
    setLoading(true);
    try {
      const [nextAlbums, nextMemories, nextMap] = await Promise.all([albums(), memories(), mapAssets()]);
      setAlbumItems(nextAlbums);
      setMemoryItems(nextMemories);
      setMapItems(nextMap);
    } catch (error) {
      setMessage(errorMessage(error));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let active = true;
    (async () => {
      const storedConnection = await isConnected();
      if (!active) return;
      if (!storedConnection) {
        setConnected(false);
        return;
      }
      setConnected(true);
      const savedPolicy = await loadBackupPolicy();
      if (active) setPolicy(savedPolicy);
      try {
        await verifyConnection();
        if (!active) return;
        setMessage("Connected. Your media library is ready.");
        await Promise.all([refreshLibrary(), refreshQueue(), configureNotifications(), setAutomaticBackup(savedPolicy.automatic)]);
      } catch (error) {
        if (!active) return;
        if (error instanceof DriveboundApiError && error.status === 401) {
          await clearConnection();
          setConnected(false);
          setMessage("This device connection was revoked. Sign in to reconnect it.");
        } else {
          setMessage(errorMessage(error));
          await refreshQueue();
        }
      }
    })();
    return () => { active = false; };
  }, [refreshLibrary, refreshQueue]);

  useEffect(() => {
    if (connected && tab === "discover") refreshDiscover();
  }, [connected, tab, refreshDiscover]);

  useEffect(() => {
    Network.getNetworkStateAsync().then(state => setNetwork(networkLabel(state)));
    const subscription = Network.addNetworkStateListener(state => {
      setNetwork(networkLabel(state));
      if (state.isConnected && connected && policy.automatic && !policy.paused && !reconnectRunRef.current) {
        reconnectRunRef.current = true;
        runBackup(undefined, { mode: "background" })
          .catch(() => undefined)
          .finally(() => {
            reconnectRunRef.current = false;
            refreshQueue();
          });
      }
    });
    return () => subscription.remove();
  }, [connected, policy.automatic, policy.paused, refreshQueue]);

  useEffect(() => {
    if (!connected) return;
    const timer = setInterval(refreshQueue, 10_000);
    const subscription = AppState.addEventListener("change", state => {
      if (state === "active") refreshQueue();
    });
    return () => {
      clearInterval(timer);
      subscription.remove();
    };
  }, [connected, refreshQueue]);

  const connect = async () => {
    setLoading(true);
    try {
      await configureDevice(server, email, password, Platform.OS === "ios" ? "My iPhone" : "My Android phone", otp || undefined);
      setConnected(true);
      setPassword("");
      setOtp("");
      setMessage("Connected. Your media library is ready.");
      const savedPolicy = await loadBackupPolicy();
      setPolicy(savedPolicy);
      await Promise.all([refreshLibrary(), refreshQueue(), configureNotifications(), setAutomaticBackup(savedPolicy.automatic)]);
    } catch (error) {
      setMessage(errorMessage(error));
    } finally {
      setLoading(false);
    }
  };

  const runNow = async () => {
    setLoading(true);
    try {
      const outcome = await runBackup(setProgress, { mode: "manual" });
      setProgress(outcome);
      if (outcome.blocked) setMessage(outcome.blocked);
      else setMessage(`Backup checked ${outcome.scanned} items: ${outcome.uploaded} uploaded, ${outcome.known + outcome.skipped} already safe.`);
      if (outcome.uploaded > 0) await refreshLibrary();
    } catch (error) {
      setMessage(errorMessage(error));
    } finally {
      setLoading(false);
      await refreshQueue();
    }
  };

  const savePolicyChange = async (change: Partial<BackupPolicy>) => {
    const next = { ...policy, ...change };
    setPolicy(next);
    await saveBackupPolicy(next);
    await setAutomaticBackup(next.automatic);
  };

  const performSearch = async () => {
    if (!query.trim()) return;
    setLoading(true);
    try {
      setActiveAlbum(null);
      setResults(await search(query));
    } catch (error) {
      setMessage(errorMessage(error));
    } finally {
      setLoading(false);
    }
  };

  const openAlbum = async (album: Album) => {
    setLoading(true);
    try {
      setActiveAlbum(album);
      setResults(await albumAssets(album.id));
    } catch (error) {
      setMessage(errorMessage(error));
    } finally {
      setLoading(false);
    }
  };

  const confirmCancelQueue = () => Alert.alert(
    "Cancel pending backups?",
    "This removes queued and failed transfers from this phone. Your photos stay on the phone and can be scanned again later.",
    [
      { text: "Keep queue", style: "cancel" },
      {
        text: "Cancel pending",
        style: "destructive",
        onPress: async () => {
          const removed = await cancelPendingBackupTransfers();
          setMessage(`${removed} pending transfer${removed === 1 ? "" : "s"} removed.`);
          await refreshQueue();
        },
      },
    ],
  );

  const disconnect = () => Alert.alert(
    "Disconnect this phone?",
    "Automatic backups stop until you sign in again. Existing server media is unchanged.",
    [
      { text: "Cancel", style: "cancel" },
      {
        text: "Disconnect",
        style: "destructive",
        onPress: async () => {
          await setAutomaticBackup(false);
          await clearConnection();
          setConnected(false);
          setLibrary([]);
          setMessage("This phone is disconnected.");
        },
      },
    ],
  );

  const mappedPlaces = useMemo(() => mapItems.slice(0, 100), [mapItems]);

  if (connected === null) return <SafeAreaView style={styles.safe}><ActivityIndicator color="#2266d5" /></SafeAreaView>;
  if (!connected) return <SafeAreaView style={styles.safe}>
    <ScrollView contentContainerStyle={styles.loginWrap} keyboardShouldPersistTaps="handled">
      <View style={styles.card}>
        <Text style={styles.brand}>Drivebound</Text>
        <Text style={styles.title}>Your media, on your storage.</Text>
        <Text style={styles.copy}>{message}</Text>
        <TextInput style={styles.input} value={server} onChangeText={setServer} autoCapitalize="none" autoCorrect={false} placeholder="Server URL" />
        <Text style={styles.serverHint}>{Platform.OS === "android" ? "Emulator: 10.0.2.2 - USB device: adb reverse" : "Use your Drivebound HTTPS or LAN address"}</Text>
        <TextInput style={styles.input} value={email} onChangeText={setEmail} autoCapitalize="none" autoComplete="email" placeholder="Email" keyboardType="email-address" />
        <TextInput style={styles.input} value={password} onChangeText={setPassword} secureTextEntry autoComplete="current-password" placeholder="Password" />
        <TextInput style={styles.input} value={otp} onChangeText={setOtp} keyboardType="number-pad" placeholder="Authenticator code (if enabled)" />
        <TouchableOpacity disabled={loading} style={[styles.primaryButton, loading && styles.disabled]} onPress={connect}>
          <Text style={styles.primaryButtonText}>{loading ? "Connecting..." : "Connect device"}</Text>
        </TouchableOpacity>
      </View>
    </ScrollView>
  </SafeAreaView>;

  return <SafeAreaView style={styles.safe}>
    <Viewer asset={selected} onClose={() => setSelected(null)} />
    <View style={styles.appHeader}>
      <View><Text style={styles.brand}>Drivebound</Text><Text style={styles.headerTitle}>{tab === "library" ? "Library" : tab === "discover" ? "Discover" : tab === "backup" ? "Backup" : "Settings"}</Text></View>
      {loading && <ActivityIndicator color="#2266d5" />}
    </View>
    <ScrollView contentContainerStyle={styles.content} keyboardShouldPersistTaps="handled">
      {message !== "Connected. Your media library is ready." && <Text style={styles.status}>{message}</Text>}

      {tab === "library" && <>
        <View style={styles.sectionHeader}><Text style={styles.sectionTitle}>All media</Text><TouchableOpacity onPress={refreshLibrary}><Text style={styles.textButton}>Refresh</Text></TouchableOpacity></View>
        <AssetGrid assets={library} onSelect={setSelected} />
        {libraryHasMore && <TouchableOpacity disabled={loading} style={styles.secondaryButton} onPress={loadMoreLibrary}><Text style={styles.secondaryButtonText}>{loading ? "Loading..." : "Load more"}</Text></TouchableOpacity>}
      </>}

      {tab === "discover" && <>
        <Text style={styles.sectionTitle}>{activeAlbum ? activeAlbum.name : "Search"}</Text>
        <View style={styles.searchRow}>
          <TextInput style={[styles.input, styles.searchInput]} value={query} onChangeText={setQuery} placeholder="Search people, places, text..." onSubmitEditing={performSearch} returnKeyType="search" />
          <TouchableOpacity style={styles.searchButton} onPress={performSearch}><Text style={styles.primaryButtonText}>Search</Text></TouchableOpacity>
        </View>
        {(results.length > 0 || activeAlbum) && <>
          <TouchableOpacity onPress={() => { setResults([]); setActiveAlbum(null); }}><Text style={styles.textButton}>Back to discovery</Text></TouchableOpacity>
          <AssetGrid assets={results} onSelect={setSelected} />
        </>}
        {!results.length && !activeAlbum && <>
          <Text style={styles.subheading}>Albums</Text>
          <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={styles.horizontal}>
            {albumItems.map(album => <TouchableOpacity key={album.id} style={styles.albumCard} onPress={() => openAlbum(album)}>
              <AuthenticatedImage path={album.cover_thumbnail_url} style={styles.albumImage} />
              <Text style={styles.albumName}>{album.name}</Text>
              <Text style={styles.albumMeta}>{album.asset_count} items - {album.role}</Text>
            </TouchableOpacity>)}
          </ScrollView>
          <Text style={styles.subheading}>Memories</Text>
          <AssetGrid assets={memoryItems} onSelect={setSelected} />
          <Text style={styles.subheading}>Map</Text>
          {mappedPlaces.map(place => <TouchableOpacity key={place.id} style={styles.place} onPress={() => Linking.openURL(`https://www.google.com/maps/search/?api=1&query=${place.latitude},${place.longitude}`)}>
            <AuthenticatedImage path={place.thumbnail_url} style={styles.placeImage} />
            <View style={styles.placeText}><Text style={styles.placeName}>{place.name}</Text><Text style={styles.albumMeta}>{place.latitude.toFixed(4)}, {place.longitude.toFixed(4)}</Text></View>
            <Text style={styles.textButton}>Open map</Text>
          </TouchableOpacity>)}
        </>}
      </>}

      {tab === "backup" && <>
        <View style={styles.connectionRow}><View style={[styles.connectionDot, network === "Offline" && styles.connectionDotOffline]} /><Text style={styles.connectionText}>{network}</Text></View>
        <Text style={styles.copy}>Unfinished uploads stay in a protected local queue and resume safely after app, network, or device restarts.</Text>
        <View style={styles.statsRow}><Stat value={summary.pending} label="Pending" /><Stat value={summary.complete} label="Protected" /><Stat value={summary.failed} label="Needs attention" /></View>
        {summary.bytesPending > 0 && <Text style={styles.queueMeta}>{formatBytes(summary.bytesPending)} remaining{summary.nextRetryAt ? ` - next retry ${new Date(summary.nextRetryAt).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}` : ""}</Text>}
        <TouchableOpacity disabled={loading || policy.paused} style={[styles.primaryButton, (loading || policy.paused) && styles.disabled]} onPress={runNow}>
          <Text style={styles.primaryButtonText}>{loading ? "Backing up..." : policy.paused ? "Backup paused" : "Back up now"}</Text>
        </TouchableOpacity>
        <View style={styles.buttonRow}>
          <TouchableOpacity style={[styles.secondaryButton, styles.rowButton]} onPress={() => savePolicyChange({ paused: !policy.paused })}><Text style={styles.secondaryButtonText}>{policy.paused ? "Resume queue" : "Pause queue"}</Text></TouchableOpacity>
          {summary.pending + summary.failed > 0 && <TouchableOpacity disabled={loading} style={[styles.secondaryButton, styles.rowButton]} onPress={confirmCancelQueue}><Text style={styles.secondaryButtonText}>Cancel pending</Text></TouchableOpacity>}
        </View>
        {summary.failed > 0 && <TouchableOpacity style={styles.secondaryButton} onPress={async () => { await retryFailedBackupTransfers(); await refreshQueue(); }}><Text style={styles.secondaryButtonText}>Retry failed media</Text></TouchableOpacity>}
        {progress && <Text style={styles.progress}>{progress.scanned} scanned - {progress.queued} queued - {progress.uploaded} uploaded - {progress.known + progress.skipped} already safe - {progress.failed} failed{progress.deferred ? ` - ${progress.deferred} deferred` : ""}{progress.current ? `\n${progress.current}` : ""}</Text>}
        {queueItems.length > 0 && <><Text style={styles.subheading}>Transfer queue</Text>{queueItems.map(item => <QueueItem key={item.localVersion} transfer={item} />)}</>}
      </>}

      {tab === "settings" && <>
        <Text style={styles.subheading}>Automatic backup</Text>
        <Setting label="Automatic backup" detail="Lets the operating system run queued backups." value={policy.automatic} onChange={value => savePolicyChange({ automatic: value })} />
        <Setting label="Wi-Fi only" detail="Avoid using cellular data." value={policy.wifiOnly} onChange={value => savePolicyChange({ wifiOnly: value })} />
        <Setting label="Only while charging" detail="Wait until a charger is connected." value={policy.chargingOnly} onChange={value => savePolicyChange({ chargingOnly: value })} />
        <Text style={styles.subheading}>Transfer limits</Text>
        <Text style={styles.fieldLabel}>Bandwidth limit in Kbps (0 is unlimited)</Text>
        <TextInput style={styles.input} value={String(policy.bandwidthKbps)} onChangeText={value => savePolicyChange({ bandwidthKbps: Number(value.replace(/\D/g, "")) || 0 })} keyboardType="number-pad" placeholder="0" />
        <Text style={styles.fieldLabel}>Automatic backup schedule (same start and end means anytime)</Text>
        <View style={styles.hourRow}>
          <TextInput style={[styles.input, styles.hourInput]} value={String(policy.scheduleStartHour)} onChangeText={value => savePolicyChange({ scheduleStartHour: Math.min(23, Number(value) || 0) })} keyboardType="number-pad" maxLength={2} />
          <Text style={styles.hourSeparator}>to</Text>
          <TextInput style={[styles.input, styles.hourInput]} value={String(policy.scheduleEndHour)} onChangeText={value => savePolicyChange({ scheduleEndHour: Math.min(23, Number(value) || 0) })} keyboardType="number-pad" maxLength={2} />
        </View>
        <Text style={styles.settingNote}>Manual backups ignore the schedule but still respect Wi-Fi, charging, and pause controls.</Text>
        <TouchableOpacity style={styles.secondaryButton} onPress={disconnect}><Text style={styles.secondaryButtonText}>Disconnect this phone</Text></TouchableOpacity>
      </>}
    </ScrollView>
    <View style={styles.tabs}>{([ ["library", "Library"], ["discover", "Discover"], ["backup", "Backup"], ["settings", "Settings"] ] as [Tab, string][]).map(([value, label]) => <TouchableOpacity key={value} style={styles.tab} onPress={() => setTab(value)} accessibilityRole="tab" accessibilityState={{ selected: tab === value }}><Text style={[styles.tabText, tab === value && styles.tabTextActive]}>{label}</Text></TouchableOpacity>)}</View>
  </SafeAreaView>;
}

function Stat({ value, label }: { value: number; label: string }) {
  return <View style={styles.stat}><Text style={styles.statValue}>{value}</Text><Text style={styles.statLabel}>{label}</Text></View>;
}

function Setting({ label, detail, value, onChange }: { label: string; detail: string; value: boolean; onChange: (value: boolean) => void }) {
  return <View style={styles.setting}><View style={styles.settingText}><Text style={styles.settingLabel}>{label}</Text><Text style={styles.settingDetail}>{detail}</Text></View><Switch value={value} onValueChange={onChange} trackColor={{ true: "#7da8f0" }} /></View>;
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: "#e8edf3" },
  loginWrap: { flexGrow: 1, justifyContent: "center", padding: 20 },
  card: { padding: 28, borderRadius: 28, backgroundColor: "#e8edf3", shadowColor: "#77889b", shadowOpacity: 0.35, shadowRadius: 18, elevation: 8 },
  appHeader: { flexDirection: "row", justifyContent: "space-between", alignItems: "center", paddingHorizontal: 20, paddingTop: 14, paddingBottom: 10 },
  content: { padding: 20, paddingTop: 8, paddingBottom: 100 },
  brand: { color: "#2266d5", fontWeight: "800", fontSize: 17, letterSpacing: 0.4 },
  title: { fontSize: 32, fontWeight: "800", color: "#172335", marginVertical: 14 },
  headerTitle: { fontSize: 28, fontWeight: "800", color: "#172335", marginTop: 2 },
  copy: { color: "#526174", lineHeight: 22, marginBottom: 18 },
  status: { color: "#526174", backgroundColor: "#f5f8fb", borderRadius: 12, padding: 12, marginBottom: 16 },
  input: { backgroundColor: "#f5f8fb", padding: 15, borderRadius: 14, marginBottom: 12, color: "#172335", fontSize: 16 },
  serverHint: { fontSize: 11, color: "#6b7888", lineHeight: 16, marginTop: -4, marginBottom: 12, paddingHorizontal: 4 },
  primaryButton: { backgroundColor: "#2266d5", padding: 16, borderRadius: 15, alignItems: "center", marginTop: 8 },
  primaryButtonText: { color: "white", fontWeight: "700" },
  secondaryButton: { borderWidth: 1, borderColor: "#b8c5d4", padding: 15, borderRadius: 15, alignItems: "center", marginTop: 12 },
  secondaryButtonText: { color: "#334258", fontWeight: "700" },
  disabled: { opacity: 0.5 },
  buttonRow: { flexDirection: "row", gap: 10 },
  rowButton: { flex: 1 },
  sectionHeader: { flexDirection: "row", justifyContent: "space-between", alignItems: "center", marginBottom: 12 },
  sectionTitle: { fontSize: 21, fontWeight: "800", color: "#172335" },
  subheading: { fontSize: 18, fontWeight: "800", color: "#172335", marginTop: 24, marginBottom: 12 },
  textButton: { color: "#2266d5", fontWeight: "700", paddingVertical: 6 },
  grid: { flexDirection: "row", flexWrap: "wrap", gap: 10 },
  assetCard: { width: "48%", backgroundColor: "#f1f5f9", borderRadius: 16, overflow: "hidden", position: "relative", marginBottom: 4 },
  assetImage: { width: "100%", height: 142 },
  image: { backgroundColor: "#d3dde8" },
  placeholder: { backgroundColor: "#d3dde8", alignItems: "center", justifyContent: "center" },
  placeholderText: { color: "#8ca0b5", fontSize: 13, fontWeight: "800" },
  assetName: { color: "#334258", fontSize: 12, paddingHorizontal: 9, paddingTop: 9 },
  assetDate: { color: "#78889a", fontSize: 10, paddingHorizontal: 9, paddingTop: 2, paddingBottom: 9 },
  videoBadge: { position: "absolute", top: 8, right: 8, backgroundColor: "#172335cc", borderRadius: 12, paddingHorizontal: 7, paddingVertical: 3 },
  videoBadgeText: { color: "white", fontSize: 9, fontWeight: "800" },
  empty: { color: "#6b7888", paddingVertical: 22, textAlign: "center" },
  searchRow: { flexDirection: "row", gap: 8, alignItems: "flex-start" },
  searchInput: { flex: 1 },
  searchButton: { backgroundColor: "#2266d5", paddingHorizontal: 14, paddingVertical: 16, borderRadius: 14 },
  horizontal: { gap: 12 },
  albumCard: { width: 150 },
  albumImage: { height: 115, width: 150, borderRadius: 14 },
  albumName: { color: "#172335", fontWeight: "800", marginTop: 7 },
  albumMeta: { color: "#6b7888", fontSize: 12, marginTop: 2 },
  place: { backgroundColor: "#f1f5f9", padding: 9, borderRadius: 15, flexDirection: "row", alignItems: "center", marginBottom: 10, gap: 10 },
  placeImage: { width: 52, height: 52, borderRadius: 11 },
  placeText: { flex: 1 },
  placeName: { color: "#172335", fontWeight: "700" },
  connectionRow: { flexDirection: "row", alignItems: "center", gap: 8, marginBottom: 10 },
  connectionDot: { width: 9, height: 9, borderRadius: 5, backgroundColor: "#2eaa68" },
  connectionDotOffline: { backgroundColor: "#d25b5b" },
  connectionText: { color: "#526174", fontWeight: "700" },
  statsRow: { flexDirection: "row", gap: 10, marginBottom: 10 },
  stat: { flex: 1, backgroundColor: "#f1f5f9", borderRadius: 15, padding: 13, alignItems: "center" },
  statValue: { fontSize: 24, color: "#172335", fontWeight: "800" },
  statLabel: { color: "#6b7888", fontSize: 11, textAlign: "center", marginTop: 3 },
  queueMeta: { color: "#6b7888", textAlign: "center", fontSize: 12, marginBottom: 4 },
  progress: { color: "#526174", lineHeight: 22, marginTop: 18, backgroundColor: "#f5f8fb", borderRadius: 12, padding: 13 },
  queueItem: { backgroundColor: "#f5f8fb", borderRadius: 13, padding: 12, marginBottom: 9 },
  queueItemHeader: { flexDirection: "row", gap: 10, justifyContent: "space-between" },
  queueName: { flex: 1, color: "#334258", fontWeight: "700" },
  queueState: { color: "#2266d5", fontSize: 11, fontWeight: "800", textTransform: "capitalize" },
  queueError: { color: "#9e4747", fontSize: 11, lineHeight: 16, marginTop: 7 },
  progressTrack: { height: 4, borderRadius: 2, backgroundColor: "#d4deea", overflow: "hidden", marginTop: 9 },
  progressFill: { height: 4, backgroundColor: "#2266d5" },
  setting: { flexDirection: "row", justifyContent: "space-between", alignItems: "center", paddingVertical: 15, borderBottomWidth: 1, borderBottomColor: "#d6dee8" },
  settingText: { flex: 1, paddingRight: 12 },
  settingLabel: { color: "#172335", fontSize: 16, fontWeight: "700" },
  settingDetail: { color: "#6b7888", fontSize: 12, marginTop: 3, lineHeight: 17 },
  settingNote: { color: "#6b7888", fontSize: 12, lineHeight: 18, marginTop: 4 },
  fieldLabel: { color: "#526174", marginBottom: 7, fontSize: 13 },
  hourRow: { flexDirection: "row", alignItems: "center", gap: 10 },
  hourInput: { flex: 1 },
  hourSeparator: { color: "#6b7888", marginBottom: 12 },
  tabs: { position: "absolute", bottom: 0, left: 0, right: 0, flexDirection: "row", backgroundColor: "#f5f8fb", borderTopWidth: 1, borderTopColor: "#d6dee8", paddingTop: 8, paddingBottom: Platform.OS === "ios" ? 22 : 10 },
  tab: { flex: 1, alignItems: "center", paddingVertical: 6 },
  tabText: { color: "#6b7888", fontSize: 12, fontWeight: "700" },
  tabTextActive: { color: "#2266d5" },
  viewerSafe: { flex: 1, backgroundColor: "#172335" },
  viewerHeader: { padding: 16, flexDirection: "row", alignItems: "center", justifyContent: "space-between" },
  viewerClose: { color: "#8fc0ff", fontWeight: "700" },
  viewerTitle: { color: "white", maxWidth: "70%", fontWeight: "700" },
  viewerHeaderSpacer: { width: 42 },
  viewerPreview: { height: "52%", justifyContent: "center", alignItems: "center" },
  viewerMedia: { width: "100%", height: "100%", backgroundColor: "#101821" },
  viewerLoading: { width: "100%", height: "100%", alignItems: "center", justifyContent: "center" },
  detailSheet: { backgroundColor: "#eef3f8" },
  detailContent: { padding: 16 },
  detailHeading: { color: "#172335", fontSize: 18, fontWeight: "800", marginBottom: 8 },
  detailRow: { flexDirection: "row", justifyContent: "space-between", gap: 16, paddingVertical: 7, borderBottomWidth: 1, borderBottomColor: "#d8e0e9" },
  detailLabel: { color: "#6b7888", fontSize: 12 },
  detailValue: { flex: 1, color: "#334258", fontSize: 12, textAlign: "right" },
  detailError: { color: "#9e4747", fontSize: 12 },
  viewerActions: { padding: 12, backgroundColor: "#eef3f8", flexDirection: "row", gap: 10 },
  actionButton: { flex: 1, marginTop: 0 },
});
