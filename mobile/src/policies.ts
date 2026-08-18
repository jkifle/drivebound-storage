import AsyncStorage from "@react-native-async-storage/async-storage";
import * as Battery from "expo-battery";
import * as Network from "expo-network";

const POLICY_KEY = "drivebound.backupPolicy.v2";

export type BackupPolicy = {
  automatic: boolean;
  paused: boolean;
  wifiOnly: boolean;
  chargingOnly: boolean;
  bandwidthKbps: number;
  scheduleStartHour: number;
  scheduleEndHour: number;
};

export const defaultBackupPolicy: BackupPolicy = {
  automatic: false,
  paused: false,
  wifiOnly: true,
  chargingOnly: false,
  bandwidthKbps: 0,
  scheduleStartHour: 0,
  scheduleEndHour: 0,
};

export async function loadBackupPolicy(): Promise<BackupPolicy> {
  const raw = await AsyncStorage.getItem(POLICY_KEY);
  if (!raw) return defaultBackupPolicy;
  try {
    return { ...defaultBackupPolicy, ...JSON.parse(raw) };
  } catch {
    return defaultBackupPolicy;
  }
}

export async function saveBackupPolicy(policy: BackupPolicy): Promise<void> {
  await AsyncStorage.setItem(POLICY_KEY, JSON.stringify({
    ...policy,
    bandwidthKbps: Math.max(0, Math.min(100_000, Math.round(policy.bandwidthKbps || 0))),
    scheduleStartHour: Math.max(0, Math.min(23, Math.round(policy.scheduleStartHour))),
    scheduleEndHour: Math.max(0, Math.min(23, Math.round(policy.scheduleEndHour))),
  }));
}

function inScheduledWindow(policy: BackupPolicy, now = new Date()): boolean {
  const start = policy.scheduleStartHour;
  const end = policy.scheduleEndHour;
  if (start === end) return true;
  const hour = now.getHours();
  return start < end ? hour >= start && hour < end : hour >= start || hour < end;
}

export async function backupPolicyBlocker(
  policy?: BackupPolicy,
  options: { mode?: "manual" | "background" } = {},
): Promise<string | null> {
  const activePolicy = policy ?? await loadBackupPolicy();
  if (activePolicy.paused) return "Backup is paused";
  if (options.mode === "background" && !inScheduledWindow(activePolicy)) {
    return `Scheduled for ${String(activePolicy.scheduleStartHour).padStart(2, "0")}:00-${String(activePolicy.scheduleEndHour).padStart(2, "0")}:00`;
  }
  const network = await Network.getNetworkStateAsync();
  // A self-hosted server may be reachable over the LAN while the phone has no
  // route to the public internet, so connectivity is the meaningful signal.
  if (!network.isConnected) return "Waiting for a network connection";
  if (
    activePolicy.wifiOnly
    && network.type !== Network.NetworkStateType.WIFI
    && network.type !== Network.NetworkStateType.ETHERNET
  ) return "Waiting for Wi-Fi";
  if (activePolicy.chargingOnly) {
    const battery = await Battery.getBatteryStateAsync();
    if (battery !== Battery.BatteryState.CHARGING && battery !== Battery.BatteryState.FULL) {
      return "Waiting until the phone is charging";
    }
  }
  return null;
}

export async function networkStatusLabel(): Promise<string> {
  const network = await Network.getNetworkStateAsync();
  if (!network.isConnected) return "Offline";
  if (network.type === Network.NetworkStateType.WIFI) return "Wi-Fi connected";
  if (network.type === Network.NetworkStateType.ETHERNET) return "Ethernet connected";
  if (network.type === Network.NetworkStateType.CELLULAR) return "Cellular connected";
  return "Network connected";
}

export async function throttleForBandwidth(
  bytes: number,
  bandwidthKbps: number,
  heartbeat?: () => Promise<void>,
): Promise<void> {
  if (bandwidthKbps <= 0) return;
  let milliseconds = Math.ceil((bytes * 8 * 1000) / (bandwidthKbps * 1000));
  while (milliseconds > 0) {
    const interval = Math.min(milliseconds, 30_000);
    await new Promise<void>(resolve => setTimeout(resolve, interval));
    milliseconds -= interval;
    if (heartbeat) await heartbeat();
  }
}
