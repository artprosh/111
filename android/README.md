# Android test client

This folder builds an installable Android APK that opens the standalone app server in a secure WebView.

The APK requires an HTTPS server URL. It does not embed production credentials or bypass server authentication. Enter the server URL on first launch. App-side push notifications and standalone account login must be implemented by the server before the client can place real orders.

## Build

From this folder, run:

    gradle assembleDebug

The APK is written to:

    app/build/outputs/apk/debug/app-debug.apk

A GitHub Actions workflow builds the same debug APK and uploads it as an artifact for 7 days.
