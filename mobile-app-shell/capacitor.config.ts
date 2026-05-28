import type { CapacitorConfig } from '@capacitor/cli';

const portalUrl = process.env.DREAMZ_PORTAL_URL?.trim();
const configuredNavigation = process.env.DREAMZ_ALLOW_NAVIGATION?.split(',')
  .map((host) => host.trim())
  .filter(Boolean);

let portalHost: string | undefined;
if (portalUrl) {
  portalHost = new URL(portalUrl).hostname;
}

const allowNavigation = configuredNavigation?.length
  ? configuredNavigation
  : portalHost
    ? [portalHost]
    : [];

const config: CapacitorConfig = {
  appId: process.env.DREAMZ_APP_ID || 'com.dreamzfitness.member',
  appName: process.env.DREAMZ_APP_NAME || 'Dreamz Fitness',
  webDir: 'www',
  backgroundColor: '#050914',
  server: {
    androidScheme: 'https',
    iosScheme: 'capacitor',
    allowNavigation,
    errorPath: 'offline.html',
    ...(portalUrl ? { url: portalUrl } : {}),
  },
  plugins: {
    SplashScreen: {
      launchAutoHide: true,
      backgroundColor: '#050914',
      androidSplashResourceName: 'splash',
      androidScaleType: 'CENTER_CROP',
      showSpinner: false,
    },
    StatusBar: {
      style: 'DARK',
      backgroundColor: '#050914',
      overlaysWebView: false,
    },
    Keyboard: {
      resize: 'body',
    },
  },
  android: {
    allowMixedContent: false,
  },
  ios: {
    contentInset: 'automatic',
    scrollEnabled: true,
  },
};

export default config;
