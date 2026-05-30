import { existsSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { spawn } from 'node:child_process';

const root = resolve(import.meta.dirname, '..');
const androidDir = resolve(root, 'android');
const gradleArgs = process.argv.slice(2);

if (gradleArgs.length === 0) {
  console.error('Usage: node scripts/run-android-gradle.mjs :app:assembleDebug');
  process.exit(1);
}

const androidStudioJava = 'C:\\Program Files\\Android\\Android Studio\\jbr';
const localAndroidSdk = join(process.env.LOCALAPPDATA ?? '', 'Android', 'Sdk');

const env = { ...process.env };
if (!env.JAVA_HOME && existsSync(join(androidStudioJava, 'bin', 'java.exe'))) {
  env.JAVA_HOME = androidStudioJava;
}

if (!env.ANDROID_HOME && existsSync(localAndroidSdk)) {
  env.ANDROID_HOME = localAndroidSdk;
}

const pathParts = [];
if (env.JAVA_HOME) pathParts.push(join(env.JAVA_HOME, 'bin'));
if (env.ANDROID_HOME) {
  pathParts.push(join(env.ANDROID_HOME, 'platform-tools'));
  pathParts.push(join(env.ANDROID_HOME, 'cmdline-tools', 'latest', 'bin'));
}
pathParts.push(env.Path ?? env.PATH ?? '');
env.Path = pathParts.join(';');

const command = `gradlew.bat ${gradleArgs.join(' ')}`;
const child = spawn('cmd.exe', ['/d', '/c', command], {
  cwd: androidDir,
  env,
  shell: false,
  stdio: 'inherit',
});

child.on('exit', (code) => {
  process.exit(code ?? 1);
});
