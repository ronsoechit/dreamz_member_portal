export const allowedMemberPaths = [
  '/',
  '/choose-language',
  '/login',
  '/dashboard',
  '/coach',
  '/nutrition',
  '/progress',
  '/account',
  '/account/*',
  '/membership-options',
  '/pricing',
  '/group-classes',
  '/apply',
];

export const blockedPaths = [
  '/staff',
  '/staff/*',
  '/admin',
  '/admin/*',
];

export function normalizePath(pathname) {
  if (!pathname || pathname === '') return '/';
  const normalized = pathname.startsWith('/') ? pathname : `/${pathname}`;
  return normalized.length > 1 ? normalized.replace(/\/+$/, '') : normalized;
}

export function pathMatchesPolicy(pathname, policyPath) {
  const path = normalizePath(pathname).toLowerCase();
  const policy = normalizePath(policyPath).toLowerCase();

  if (policy.endsWith('/*')) {
    const prefix = policy.slice(0, -2);
    return path === prefix || path.startsWith(`${prefix}/`);
  }

  return path === policy;
}

export function isBlockedPath(pathname) {
  return blockedPaths.some((policyPath) => pathMatchesPolicy(pathname, policyPath));
}

export function isAllowedMemberPath(pathname) {
  return allowedMemberPaths.some((policyPath) => pathMatchesPolicy(pathname, policyPath));
}
