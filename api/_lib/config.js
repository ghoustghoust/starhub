const DEFAULT_ORIGIN = 'https://ghoustghoust.github.io';

export const ALLOWED_ORIGINS = new Set(
  (process.env.ALLOWED_ORIGINS || DEFAULT_ORIGIN)
    .split(',')
    .map((origin) => origin.trim().toLowerCase())
    .filter(Boolean),
);

export const GITHUB_REPOSITORY = process.env.GITHUB_REPOSITORY || 'ghoustghoust/starhub';
export const GH_TOKEN = process.env.GH_TOKEN || '';
export const SITE_ORIGIN = process.env.SITE_ORIGIN || DEFAULT_ORIGIN;
