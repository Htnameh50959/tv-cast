# TV Cast — Android

A minimal Android app (Java, no AndroidX) that discovers LG webOS TVs on the home Wi-Fi, opens a local HTTP page on the TV, and streams the device's screen to it via MJPEG over HTTP.

## Design choices (as requested)

- **MJPEG over HTTP** — not H.264/fMP4 + MSE. `MediaProjection` → `ImageReader` → JPEG is simpler and lower-risk. The Linux app already has an MJPEG fallback path.
- **Java** — avoids Kotlin/AGP/Gradle compatibility risk. No AndroidX (`android.app.Activity`), hand-rolled minimal WebSocket client + `org.json` (built into Android) so there are no third-party dependencies.
- **Foreground service** with `FOREGROUND_SERVICE_MEDIA_PROJECTION` on Android 14+. Broadcasts status to the Activity via explicit package broadcasts; client-key persisted in `SharedPreferences`.
- **SSDP discovery** (`M-SEARCH`, `ST: urn:lge-com:service:webos-second-screen:1`), **SSAP** registration + `open`/`close` payloads, reused from the Linux `netcast.py`/`lg_handshake.py`.

## Project structure

```
android/
├── settings.gradle
├── build.gradle              (root, AGP 8.13.0)
├── gradle.properties
└── app/
    ├── build.gradle          (compileSdk 36, minSdk 26, namespace local.hemanth.tvcast)
    ├── AndroidManifest.xml   (INTERNET, FOREGROUND_SERVICE, POST_NOTIFICATIONS, etc.)
    ├── res/
    │   ├── values/strings.xml
    │   └── drawable/ic_cast.xml
    └── java/local/hemanth/tvcast/
        ├── MainActivity.java      (discovery UI, start/stop casting)
        ├── CastService.java       (foreground service, MediaProjection, MJPEG server on 8090)
        ├── Ssdp.java              (SSDP discovery)
        ├── WebSocket.java         (minimal RFC 6455 client, 3000 port)
        ├── Ssap.java              (LG webOS SSAP: register, open URL, close browser)
        ├── TvPage.java            (HTML page served to TV)
        ├── MjpegServer.java       (local HTTP server: /, /mjpeg, /frame, /hello)
        ├── ScreenCapture.java     (MediaProjection → RGBA → JPEG → publish)
        └── (resources)
```

## Building

The Gradle wrapper was generated with `gradle wrapper`. Build:

```bash
cd android
./gradlew assembleDebug   # or: ~/.gradle/wrapper/dists/gradle-9.3.1-bin/*/gradle-9.3.1/bin/gradle assembleDebug
```

The APK appears at `android/app/build/outputs/apk/debug/app-debug.apk`.

## Running (no device to test against)

- The app requires an LG webOS TV on the same Wi-Fi to complete the flow (SSDP → SSAP registration → open URL → MJPEG stream).
- On first use, the TV shows a pairing prompt: **press OK on the TV remote**.
- The client key is saved to `SharedPreferences` and reused on subsequent runs.
- No TV or Android device was available to verify the APK; the build passes `assembleDebug` successfully.

## Limitations (no device to test)

- Screen capture via `MediaProjection` works on Android 5.0+ but the foreground-service + media-projection sequencing on Android 14+ was verified only by code inspection.
- The SSAP protocol's exact pairing flow can vary per TV firmware; the registration payload mirrors the Linux app's known-good payload from `lg_handshake.py`.
- No third-party dependencies are used (plain Android APIs, `org.json`).