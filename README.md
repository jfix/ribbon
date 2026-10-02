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
- Tags, cover art and chapters are read in the browser with music-metadata, with a fallback parser for MP4 chapters that music-metadata misses: Nero-style `chpl` atoms and QuickTime-style chapter text tracks (what Audible-derived m4b files use).
- Lock-screen and headphone controls use the Media Session API.
- Listening history is recorded automatically per book in IndexedDB (`sessions` store). A sitting is continuous listening to one book; resuming within 10 minutes continues it, skips and scrubbing don't count, and sittings under 15 seconds are dropped. Each book also keeps first/last listened times and total time listened. Tapping a sitting on the book page jumps back to where it began.
