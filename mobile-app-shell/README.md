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

iPhone/iOS requires macOS with Xcode:

```bash
npm install
npm run add:ios
npm run sync
npm run open:ios
```

## Update flow

Most portal UI/content changes do not require a new app-store release because the app loads the live web portal. Native-only changes, app icon/splash changes, push notifications, biometric login or stricter native navigation controls do require rebuilding and releasing the app.
