# Ribbon

A phone-first audiobook player for books you own. Import an m4b or a folder of chapter files, play offline, and pick up exactly where you paused. Installs as a web app on Android and accepts audio files shared from other apps.

Live at https://audiobooks.jfix.com

## Layout

- `public/index.html` — the whole app, one file, no build step
- `public/sw.js` — service worker: offline shell, cached fonts and tag reader, share-target inbox
- `public/manifest.json` — web app manifest with the share target
- `public/icon-*.png` — app icons
- `public/_headers` — cache and content-type rules for Cloudflare
- `wrangler.jsonc` — Cloudflare Worker config (static assets, custom domain)

## Run locally

Any static server works. Service worker features need `localhost` or https.

```bash
python3 -m http.server 8765 --directory public
```

## Deploy

```bash
npx wrangler deploy
```

## How it works

- Books are copied into the browser's IndexedDB on import. Playback position is saved in localStorage every few seconds and on pause.
- Tags, cover art and chapters are read in the browser with music-metadata, with a fallback parser for Nero-style `chpl` chapter atoms.
- Lock-screen and headphone controls use the Media Session API.
