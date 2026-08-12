import * as Notifications from "expo-notifications";
import { Platform } from "react-native";

import { registerPushToken } from "./api";

Notifications.setNotificationHandler({
  handleNotification: async () => ({ shouldShowBanner: true, shouldShowList: true, shouldPlaySound: true, shouldSetBadge: false }),
});

export async function configureNotifications(): Promise<void> {
  if (Platform.OS === "android") {
    await Notifications.setNotificationChannelAsync("backups", {
      // Leaving sound unset selects Android's normal notification sound. Passing
      // "default" here is interpreted as the name of a bundled custom file.
      name: "Backups", importance: Notifications.AndroidImportance.DEFAULT, vibrationPattern: [0, 250, 250, 250],
    });
  }
  const existing = await Notifications.getPermissionsAsync();
  const permission = existing.granted ? existing : await Notifications.requestPermissionsAsync();
  if (!permission.granted) return;
  try {
    const token = (await Notifications.getExpoPushTokenAsync()).data;
    await registerPushToken(token);
  } catch {
    // Native development builds without an EAS project can still receive local
    // backup notifications; remote delivery is enabled on the store build.
  }
}

export async function notifyBackupResult(title: string, body: string): Promise<void> {
  const permissions = await Notifications.getPermissionsAsync();
  if (!permissions.granted) return;
  await Notifications.scheduleNotificationAsync({ content: { title, body, sound: "default", data: { kind: "backup" } }, trigger: null });
}
