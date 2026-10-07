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

## Adding chapters to a book that announces them

`tools/chapterize.py` adds chapter markers to an audiobook file whose narrator says "Chapter 12", "Chapitre douze" or "Zwölftes Kapitel". It runs on a Mac, offline, and never modifies the original.

```bash
brew install ffmpeg whisper-cpp
python3 tools/chapterize.py "Some Book.mp3"
```

1. The book is transcribed locally with whisper.cpp. The transcript is cached, so later runs on the same file are instant. The language is detected; override it with `--lang en`, `fr` or `de`.
2. Spoken headings at the start of a sentence become chapters, with the spoken title when there is one. Prologue, epilogue and part headings count too.
3. Each mark is placed in the pause just before the announcement.
4. The numbering is checked. Missing or duplicate chapters are reported, and a chapter heard only mid-sentence is marked for you to check.
5. The result is written to `Some Book (chapters).mp3`, plus an editable `Some Book (chapters).json`.

To correct anything, edit the JSON (titles, start times, add or delete entries) and apply it:

```bash
python3 tools/chapterize.py "Some Book.mp3" --chapters "Some Book (chapters).json"
```

Use `--dry-run` to only produce the list, `--model small` or `--accurate` for harder recordings.

## How it works

- Books are copied into the browser's IndexedDB on import. Playback position is saved in localStorage every few seconds and on pause.
- Tags, cover art and chapters are read in the browser with music-metadata, with a fallback parser for MP4 chapters that music-metadata misses: Nero-style `chpl` atoms and QuickTime-style chapter text tracks (what Audible-derived m4b files use).
- Lock-screen and headphone controls use the Media Session API.
- Listening history is recorded automatically per book in IndexedDB (`sessions` store). A sitting is continuous listening to one book; resuming within 10 minutes continues it, skips and scrubbing don't count, and sittings under 15 seconds are dropped. Each book also keeps first/last listened times and total time listened. Tapping a sitting on the book page jumps back to where it began.
