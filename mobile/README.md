# Drivebound mobile backup

This Expo client registers a revocable device credential, browses the Drivebound library, and uses a SQLite-backed queue to resume chunked camera-roll uploads. Capture time, modification time, EXIF, GPS, filenames, and the untouched original bytes flow through the same server ingestion pipeline as web uploads.

Use `npm install`, then `npx expo run:android` or `npx expo run:ios`. Background tasks and push notifications require a development/native build; they do not run in Expo Go. A physical phone must use the computer's LAN or HTTPS address instead of `localhost`.

Copy `.env.example` to a local ignored environment file when you want a build-time default server. `EXPO_PUBLIC_API_URL` is embedded in the application and must contain only a public URL, never a credential. Development builds fall back to the Android emulator/iOS localhost addresses. Production builds have no insecure fallback and require an HTTPS URL from the EAS production environment or from the connection screen.

The Backup tab exposes Wi-Fi-only, charging-only, bandwidth, and schedule controls. The Discover tab exposes search, albums, memories, and location links. Opening media supports video playback, sharing, and downloading an original to the phone library. See [RELEASE.md](RELEASE.md) for TestFlight, Play, and signing setup.

The committed Android release manifest disables cleartext traffic; its debug-only manifest permits local HTTP for emulator/LAN development. Drivebound requests image/video media access, embedded media-location access, and notifications, but explicitly blocks audio-library permission. iOS usage descriptions distinguish reading media for backup from adding an original during an explicit restore.

For the standard Android emulator, use `http://10.0.2.2:8000`; Android reserves `10.0.2.2` as the host computer's loopback address. For an emulator or USB-connected device, `adb reverse tcp:8000 tcp:8000` also makes `http://localhost:8000` reach the host backend. The reverse tunnel must be recreated after the emulator/device or ADB server restarts.

## Windows Android requirements

- `JAVA_HOME` must point to JDK 17 or newer. Android Studio's bundled runtime is supported: `C:\Program Files\Android\Android Studio\jbr`.
- `ANDROID_HOME` should point to the installed SDK, normally `%LOCALAPPDATA%\Android\Sdk`.
- `android/local.properties` is machine-local and git-ignored; it supplies `sdk.dir` for Gradle.
- The Android build uses the Windows temp directory for CMake staging so React Native's generated object paths remain below the legacy 260-character limit.
