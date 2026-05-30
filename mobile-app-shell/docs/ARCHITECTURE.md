# Dreamz Fitness Member App Shell Architecture

## 1. Current Approach

The mobile app is a remote member portal shell.

The Capacitor container loads the live Dreamz Fitness member portal URL from environment/config (`DREAMZ_PORTAL_URL`). The existing portal/backend remains the source of truth and continues to own:

- language selection
- login/auth
- member sessions
- dashboard
- coach
- nutrition
- progress
- account
- pricing
- agreements
- cancellation
- group classes
- all i18n

The shell must not duplicate portal logic. It only provides native app container behavior:

- app icon
- splash screen
- safe area
- session persistence/cookies
- deep link handling
- allowed navigation restrictions
- future push notification foundation
- future biometric/secure storage foundation

## 2. Why

This approach lets portal improvements appear in the Android/iPhone app without rebuilding duplicate native screens. Members use the same accounts, database records, workouts, coaching data, group classes, progress data, pricing, agreements and account state as the website.

The web portal stays the product surface. The native app stays a focused container around the member portal.

## 3. Capacitor `server.url` Note

Capacitor documents `server.url` as a way to load an external URL in the WebView for live-reload servers and says it is not intended for production use. See the Capacitor configuration docs: https://capacitorjs.com/docs/config

If this project uses `server.url` with the live portal in production, that is a deliberate remote-shell decision, not a standard bundled-web-assets Capacitor app.

Tradeoffs:

- Portal changes can appear in the mobile app without rebuilding native screens.
- The app requires a reliable network connection to load the portal.
- App startup depends on portal availability and performance.
- Native app-store review can pay closer attention because most UI comes from the web.
- Runtime route restrictions must be implemented and tested carefully in native Android/iOS once those projects are generated.
- Server-side auth and authorization remain mandatory. Native route guards are additional containment, not the security source of truth.

## 4. Guardrails

- Do not build native duplicate login screens.
- Do not build native duplicate language screens.
- Do not build native duplicate member pages.
- Do not include staff/admin as a mobile app experience.
- The app shell must restrict navigation to the allowed member domain and member routes.
- If a user tries a staff/admin route, the app must block it or redirect to member dashboard/login.
- External links should open outside the app or be blocked according to the allowlist.
- Language flow is handled by the portal.
- The app may pass device locale only as a hint, not as a separate native language system.
- The portal/backend remains responsible for all i18n and must avoid mixed-language pages.
- The portal/backend remains responsible for member auth, sessions and permission checks.

## 5. Allowed Member Paths

These paths are allowed inside the app shell:

- `/`
- `/choose-language`
- `/login`
- `/dashboard`
- `/coach`
- `/nutrition`
- `/progress`
- `/account`
- `/account/*`
- `/membership-options`
- `/pricing`
- `/group-classes`
- `/apply` if public/member onboarding is intended

The same path policy is stored in `mobile-navigation-policy.json` and is validated by `npm run check`.

## 6. Blocked Paths

These paths are blocked inside the app shell:

- `/staff`
- `/staff/*`
- `/admin`
- `/admin/*`
- any non-Dreamz domain unless explicitly allowed

Android now implements runtime navigation interception in `android/app/src/main/java/com/dreamzfitness/member/MainActivity.java`. iOS must receive equivalent interception before iPhone app-store testing.

`npm run check` enforces that `DREAMZ_PORTAL_URL` starts on an allowed member route and that `DREAMZ_ALLOW_NAVIGATION` does not use wildcards, schemes or paths.

Verified Android app links require a matching `/.well-known/assetlinks.json` file on the portal domain for the final release signing certificate. That file belongs to the portal/deployment side, not the shell.

## 7. Next Steps

1. Fill the real portal URL in `.env`.
2. Add Android project.
3. Test Android emulator/device.
4. Add iOS project later on macOS/Xcode.
5. Add app icon/splash.
6. Test language persistence.
7. Test login persistence.
8. Test blocked staff/admin routes.
9. Test deep links.
