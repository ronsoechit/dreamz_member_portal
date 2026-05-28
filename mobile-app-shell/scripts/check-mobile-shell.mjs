import { existsSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import {
  allowedMemberPaths,
  blockedPaths,
  isAllowedMemberPath,
  isBlockedPath,
} from './mobile-shell-policy.mjs';

const root = resolve(import.meta.dirname, '..');
const requiredFiles = [
  'package.json',
  'capacitor.config.ts',
  'mobile-navigation-policy.json',
  'docs/ARCHITECTURE.md',
  'www/index.html',
  'www/offline.html',
];

let failed = false;

for (const file of requiredFiles) {
  const path = resolve(root, file);
  if (!existsSync(path)) {
    console.error(`Missing ${file}`);
    failed = true;
  }
}

const portalUrl = process.env.DREAMZ_PORTAL_URL?.trim();
if (!portalUrl) {
  console.warn('DREAMZ_PORTAL_URL is not set. The native app will show the local setup screen until a portal URL is configured.');
} else {
  try {
    const url = new URL(portalUrl);
    if (url.protocol !== 'https:') {
      console.error('DREAMZ_PORTAL_URL must use HTTPS for production mobile builds.');
      failed = true;
    }

    const lowerPath = url.pathname.toLowerCase();
    if (isBlockedPath(lowerPath)) {
      console.error(`DREAMZ_PORTAL_URL points to a blocked route: ${url.pathname}`);
      failed = true;
    }

    if (!isAllowedMemberPath(lowerPath)) {
      console.error(`DREAMZ_PORTAL_URL must start on an allowed member route. Received: ${url.pathname}`);
      console.error(`Allowed routes: ${allowedMemberPaths.join(', ')}`);
      failed = true;
    }
  } catch {
    console.error('DREAMZ_PORTAL_URL is not a valid URL.');
    failed = true;
  }
}

const allowNavigation = process.env.DREAMZ_ALLOW_NAVIGATION?.split(',')
  .map((host) => host.trim())
  .filter(Boolean) ?? [];

for (const host of allowNavigation) {
  if (host.includes('*')) {
    console.error('DREAMZ_ALLOW_NAVIGATION must not use wildcard hosts.');
    failed = true;
  }

  if (host.includes('/') || host.includes(':')) {
    console.error(`DREAMZ_ALLOW_NAVIGATION entries must be hostnames only, not schemes or paths. Received: ${host}`);
    failed = true;
  }
}

const policyPath = resolve(root, 'mobile-navigation-policy.json');
if (existsSync(policyPath)) {
  const policy = JSON.parse(readFileSync(policyPath, 'utf8'));
  const policyAllowed = JSON.stringify(policy.allowedMemberPaths);
  const policyBlocked = JSON.stringify(policy.blockedPaths);

  if (policyAllowed !== JSON.stringify(allowedMemberPaths)) {
    console.error('mobile-navigation-policy.json allowedMemberPaths is out of sync with scripts/mobile-shell-policy.mjs.');
    failed = true;
  }

  if (policyBlocked !== JSON.stringify(blockedPaths)) {
    console.error('mobile-navigation-policy.json blockedPaths is out of sync with scripts/mobile-shell-policy.mjs.');
    failed = true;
  }
}

const packageJson = JSON.parse(readFileSync(resolve(root, 'package.json'), 'utf8'));
const requiredDependencies = [
  '@capacitor/core',
  '@capacitor/cli',
  '@capacitor/android',
  '@capacitor/ios',
];

for (const dependency of requiredDependencies) {
  if (!packageJson.dependencies?.[dependency] && !packageJson.devDependencies?.[dependency]) {
    console.error(`Missing dependency ${dependency}`);
    failed = true;
  }
}

if (failed) {
  process.exit(1);
}

console.log('Dreamz member app shell check passed.');
