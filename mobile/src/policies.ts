import AsyncStorage from "@react-native-async-storage/async-storage";
import * as Battery from "expo-battery";
import * as Network from "expo-network";

const POLICY_KEY = "drivebound.backupPolicy.v2";

export type BackupPolicy = {
  automatic: boolean;
  wifiOnly: boolean;
  chargingOnly: boolean;
  bandwidthKbps: number;
  scheduleStartHour: number;
  scheduleEndHour: number;
};

export const defaultBackupPolicy: BackupPolicy = {
  automatic: false,
  wifiOnly: true,
  chargingOnly: false,
  bandwidthKbps: 0,
  scheduleStartHour: 0,
  scheduleEndHour: 0,
};

export async function loadBackupPolicy(): Promise<BackupPolicy> {
  const raw = await AsyncStorage.getItem(POLICY_KEY);
  if (!raw) return defaultBackupPolicy;
  try { return { ...defaultBackupPolicy, ...JSON.parse(raw) }; }
  catch { return defaultBackupPolicy; }
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

export async function backupPolicyBlocker(policy?: BackupPolicy): Promise<string | null> {
  const activePolicy = policy ?? await loadBackupPolicy();
  if (!inScheduledWindow(activePolicy)) return `Scheduled for ${String(activePolicy.scheduleStartHour).padStart(2, "0")}:00–${String(activePolicy.scheduleEndHour).padStart(2, "0")}:00`;
  const network = await Network.getNetworkStateAsync();
  if (network.isInternetReachable === false || !network.isConnected) return "Waiting for an internet connection";
  if (activePolicy.wifiOnly && network.type !== Network.NetworkStateType.WIFI && network.type !== Network.NetworkStateType.ETHERNET) return "Waiting for Wi-Fi";
  if (activePolicy.chargingOnly) {
    const battery = await Battery.getBatteryStateAsync();
    if (battery !== Battery.BatteryState.CHARGING && battery !== Battery.BatteryState.FULL) return "Waiting until the phone is charging";
  }
  return null;
}

export async function throttleForBandwidth(bytes: number, bandwidthKbps: number): Promise<void> {
  if (bandwidthKbps <= 0) return;
  const milliseconds = Math.ceil((bytes * 8 * 1000) / (bandwidthKbps * 1000));
  if (milliseconds > 0) await new Promise<void>(resolve => setTimeout(resolve, milliseconds));
}
