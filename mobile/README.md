# Drivebound mobile backup

This Expo client registers a revocable device credential, scans the native photo library, and resumes chunked uploads. Capture time, modification time, EXIF, GPS, filenames, and the untouched original bytes flow through the same server ingestion pipeline as web uploads.

Use `npm install`, then `npx expo run:android` or `npx expo run:ios`. Background tasks require a development/native build; they do not run in Expo Go. A physical phone must use the computer's LAN or HTTPS address instead of `localhost`.

For the standard Android emulator, use `http://10.0.2.2:8000`; Android reserves `10.0.2.2` as the host computer's loopback address. For an emulator or USB-connected device, `adb reverse tcp:8000 tcp:8000` also makes `http://localhost:8000` reach the host backend. The reverse tunnel must be recreated after the emulator/device or ADB server restarts.

## Windows Android requirements

- `JAVA_HOME` must point to JDK 17 or newer. Android Studio's bundled runtime is supported: `C:\Program Files\Android\Android Studio\jbr`.
- `ANDROID_HOME` should point to the installed SDK, normally `%LOCALAPPDATA%\Android\Sdk`.
- `android/local.properties` is machine-local and git-ignored; it supplies `sdk.dir` for Gradle.
- The Android build uses the Windows temp directory for CMake staging so React Native's generated object paths remain below the legacy 260-character limit.
