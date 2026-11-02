# Android test client

This folder builds an installable Android APK that opens the standalone app server in a secure WebView.

The APK requires an HTTPS server URL. It does not embed production credentials or bypass server authentication. Enter the server URL on first launch.

## Server setup for real orders

The standalone server uses the existing SQLite schema and does not start a Telegram bot. On Amvera, the persistent `/data` volume keeps the current database if this project is configured on the same application. Set these secrets in the hosting dashboard, never in GitHub files:

* `APP_ADMIN_ID` — the existing numeric admin ID (or retain `ADMIN_ID`);
* `APP_ADMIN_PHONE` — phone for the first administrator, in `+7...` format;
* `APP_AUTH_SECRET` — random secret with at least 32 characters;
* `SMSRU_API_ID` — SMS.RU API key;
* `DANGER_PASSWORD` — a newly generated secret for protected admin actions; do not reuse the old repository password;
* `APP_PUBLIC_URL` — the HTTPS address used by the APK.

The server starts only when the SMS service and admin settings are configured. Existing customers' past Telegram orders are not automatically attached to a new phone account. Product photos and payment QR images that were stored only as Telegram file IDs need to be uploaded again. Push notifications, so users can be alerted while the app is closed, still need a Firebase project and server credentials; until then, in-app updates work while the app is open.

## Build

From this folder, run:

    gradle assembleDebug

The APK is written to:

    app/build/outputs/apk/debug/app-debug.apk

A GitHub Actions workflow builds the same debug APK and uploads it as an artifact for 7 days.
