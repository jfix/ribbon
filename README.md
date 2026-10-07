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

`tools/chapterize.py` adds chapter markers to an audiobook whose narrator says "Chapter 12", "Chapitre douze" or "Zwölftes Kapitel". It runs on a Mac, offline, and never modifies the original.

```bash
brew install ffmpeg whisper-cpp
python3 tools/chapterize.py "Some Book.mp3" --review
```

1. The book is transcribed locally with whisper.cpp. The transcript is cached, so later runs on the same file are instant. The language is detected; override it with `--lang en`, `fr` or `de`.
2. Spoken headings at the start of a sentence become chapters, with the spoken title when there is one. Prologue, epilogue and part headings count too.
3. Every heading, and every long pause inside a gap in the numbering, is heard again with a larger model. That fixes misheard titles and finds chapters the first pass missed. Where the two listens disagree, both versions are kept for you to choose.
4. Each mark is placed half a second before the voice returns after the longest pause near the heading.
5. With `--review`, a page opens in your browser. It shows the sound around every mark, plays exactly from it, and lets you drag or nudge marks, snap them to the pause, fix titles, add or remove chapters, and tick each one off. Edits save as you go. "Write audio file" writes the result.
6. Without `--review`, the result is written straight away. Either way you get `Some Book (chapters).m4b` and an editable `Some Book (chapters).json`.

### Why m4b

The output is an m4b with AAC audio (64 kbps by default, Apple's encoder on macOS). Players jump to any time in an m4b exactly. In long variable-bitrate mp3s, browsers and many players estimate where a time is, and land up to a minute or more away, so chapter jumps and resume points come out wrong even when the chapter marks are right. The AAC audio is encoded once per book and cached, so writing again after edits takes seconds.

`--format mp3` keeps the original audio and writes ID3 chapter frames instead, with that caveat.

### Other options

- `--chapters LIST.json` writes the file from an edited list without detecting again.
- `--dry-run` produces the list without writing audio.
- `--model small`, `--accurate` or `--second-model` trade speed for transcription accuracy.
- `--bitrate 96k` for higher quality AAC.

Transcripts, models and encoded audio are cached in `~/Library/Caches/ribbon-chapterize`. Delete that folder to reclaim the space.

## How it works

- Books are copied into the browser's IndexedDB on import. Playback position is saved in localStorage every few seconds and on pause.
- Tags, cover art and chapters are read in the browser with music-metadata, with a fallback parser for MP4 chapters that music-metadata misses: Nero-style `chpl` atoms and QuickTime-style chapter text tracks (what Audible-derived m4b files use).
- Lock-screen and headphone controls use the Media Session API.
- Listening history is recorded automatically per book in IndexedDB (`sessions` store). A sitting is continuous listening to one book; resuming within 10 minutes continues it, skips and scrubbing don't count, and sittings under 15 seconds are dropped. Each book also keeps first/last listened times and total time listened. Tapping a sitting on the book page jumps back to where it began.
