import type { ConfigContext, ExpoConfig } from "expo/config";

const PRODUCTION_BUNDLE_ID = "com.jkifle.driveboundbackup";

export default ({ config }: ConfigContext): ExpoConfig => {
  const appEnvironment = process.env.APP_ENV ?? process.env.EXPO_PUBLIC_APP_ENV ?? "development";
  const isDevelopment = appEnvironment === "development";
  const isProduction = appEnvironment === "production";
  const publicApiUrl = process.env.EXPO_PUBLIC_API_URL?.trim() || null;

  if (isProduction && publicApiUrl && !publicApiUrl.startsWith("https://")) {
    throw new Error("Production EXPO_PUBLIC_API_URL must use https://");
  }

  // Expo's runtime schema supports usesCleartextTraffic even though the SDK 57
  // TypeScript declaration has not yet exposed it on the Android config type.
  const android = {
    ...config.android,
    package: PRODUCTION_BUNDLE_ID,
    versionCode: 1,
    usesCleartextTraffic: isDevelopment,
    permissions: [
      "android.permission.ACCESS_MEDIA_LOCATION",
    ],
    blockedPermissions: [
      "android.permission.READ_MEDIA_AUDIO",
    ],
  } as NonNullable<ExpoConfig["android"]> & { usesCleartextTraffic: boolean };

  return {
    ...config,
    name: "Drivebound",
    slug: "drivebound",
    scheme: "drivebound",
    version: "0.2.0",
    runtimeVersion: { policy: "appVersion" },
    extra: {
      ...config.extra,
      appEnvironment,
      // This value is intentionally public and is embedded in the client. It
      // must only be a server URL, never an API key, password, or token.
      publicApiUrl,
    },
    android,
    ios: {
      ...config.ios,
      bundleIdentifier: PRODUCTION_BUNDLE_ID,
      buildNumber: "1",
      supportsTablet: true,
      config: {
        ...config.ios?.config,
        usesNonExemptEncryption: false,
      },
      infoPlist: {
        ...config.ios?.infoPlist,
        NSPhotoLibraryUsageDescription: "Drivebound reads the photos and videos you choose so it can back up the original files to your Drivebound server.",
        NSPhotoLibraryAddUsageDescription: "Drivebound adds an original photo or video to your library only when you choose Save original.",
        NSLocalNetworkUsageDescription: "Drivebound connects to the Drivebound server you select on your local network.",
        UIBackgroundModes: ["processing", "remote-notification"],
        BGTaskSchedulerPermittedIdentifiers: ["com.expo.modules.backgroundtask.processing"],
        ...(isDevelopment ? {
          NSAppTransportSecurity: { NSAllowsLocalNetworking: true },
        } : {
          NSAppTransportSecurity: { NSAllowsLocalNetworking: false },
        }),
      },
    },
  };
};
