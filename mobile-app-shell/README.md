# Dreamz Fitness Member App Shell

This folder is a separate Capacitor shell for the downloadable Android and iPhone member app.

The existing Flask member portal stays the source of truth. The mobile app opens the live member portal URL, so members use the same accounts, database, workouts, group classes, progress, account data and coaching data as the website.

See the architecture decision in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) before generating Android/iOS projects.

## Why this is safe

- No existing portal files are changed by this shell.
- The app shell lives in `mobile-app-shell/`.
- The native app uses the existing portal over HTTPS.
- Web changes become visible in the app because the app loads the live portal.
- Staff/admin is not built as a separate native experience. The app starts at the member login URL.

Important: this shell is a member app entry point, not a server-side security boundary. Staff/admin routes must remain protected by the existing web authentication and authorization. Native route guards must be added and tested after generating the Android/iOS projects.

## First setup

```powershell
cd mobile-app-shell
copy .env.example .env
```

Edit `.env`:

```powershell
DREAMZ_PORTAL_URL=https://your-real-portal-domain/login?source=native_app&audience=member
DREAMZ_ALLOW_NAVIGATION=your-real-portal-domain
```

Then install dependencies:

```powershell
npm.cmd install
```

Check the shell config:

```powershell
npm.cmd run check
```

`npm.cmd run check` validates the architecture guardrails in `mobile-navigation-policy.json`, including the configured start URL and navigation allowlist.

## Add native projects

Do not generate Android/iOS projects until `DREAMZ_PORTAL_URL` is configured with the real member portal URL.

Android:

```powershell
npm.cmd run add:android
npm.cmd run sync
npm.cmd run open:android
```

The Android project is now part of the app shell because it contains native member-only route protection and Dreamz branding assets. After changing `.env` or Capacitor config, run:

```powershell
npm.cmd run sync
```

To build a local debug APK:

```powershell
npm.cmd run android:debug
```

To build a release bundle for Google Play after signing is configured:

```powershell
npm.cmd run android:bundle
```

iPhone/iOS requires macOS with Xcode:

```bash
npm install
npm run add:ios
npm run sync
npm run open:ios
```

## Update flow

Most portal UI/content changes do not require a new app-store release because the app loads the live web portal. Native-only changes, app icon/splash changes, push notifications, biometric login or stricter native navigation controls do require rebuilding and releasing the app.

## Android native guard

`android/app/src/main/java/com/dreamzfitness/member/MainActivity.java` enforces the member-only shell decision:

- `dreamzfitness.app` member routes stay inside the app.
- `/staff`, `/staff/*`, `/admin` and `/admin/*` are redirected to member login.
- Unknown same-domain routes are redirected to member login until explicitly allowed.
- Non-Dreamz domains are delegated to Android as external links.

If the portal adds a new member route that must be available inside the app, add it to both `mobile-navigation-policy.json` and the Android guard before publishing.

## Android app links

The Android manifest includes an HTTPS app-link intent filter for `dreamzfitness.app`. For verified app links in production, the portal must serve a matching `/.well-known/assetlinks.json` file for the final release signing certificate.

## Android release signing

Do not commit release keys or passwords.

When ready for Google Play, create `android/keystore.properties` from `android/keystore.properties.example` and point it to the release keystore. The Gradle release config reads that local file when present. Without it, debug builds keep working, but release app bundles are unsigned and not Play-ready.
