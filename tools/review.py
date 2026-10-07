"""Local review page for chapterize.py: check every chapter mark by ear, adjust it, then write the file.

Started by `chapterize.py BOOK.mp3 --review`. Runs only on this Mac (127.0.0.1) until you press Ctrl+C.

Every sound the page plays is a short clip cut by ffmpeg exactly at the requested time, never a seek inside the
long file, because browsers can land a minute away from a seek target in long variable-bitrate mp3s. What you hear
on this page is where the mark really is.
"""
import http.server
import json
import math
import os
import struct
import subprocess
import sys
import threading
import urllib.parse
import webbrowser

import chapterize as cz


def serve(src, list_path, out, fmt, bitrate, total, work, port=0, open_browser=True):
    page_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'review.html')
    tags = probe_tags(src)
    segments = []
    tpath = os.path.join(work, 'transcript.json')
    if os.path.isfile(tpath):
        try:
            for s in json.load(open(tpath))['transcription']:
                segments.append((s['offsets']['from'] / 1000, s['text'].strip()))
        except (KeyError, ValueError):
            segments = []
    state = {'phase': 'idle', 'progress': 0.0, 'message': '', 'out': out}
    encode_lock = threading.Lock()
    save_lock = threading.Lock()

    def set_state(**kw):
        state.update(kw)

    def prepare():
        """Encode the AAC audio in the background while you review, so writing the m4b is quick."""
        if fmt != 'm4b' or os.path.isfile(os.path.join(work, f'audio-{bitrate}.m4a')):
            return
        with encode_lock:
            if state['phase'] in ('idle',):
                set_state(phase='preparing', progress=0.0, message='Preparing the m4b audio')
            try:
                cz.ensure_aac(src, work, bitrate, lambda p: state.update(progress=p) if state['phase'] == 'preparing' else None)
                if state['phase'] == 'preparing':
                    set_state(phase='idle', progress=1.0, message='')
            except Exception as e:  # noqa: BLE001 - reported to the page
                set_state(phase='error', message=f'Could not prepare the audio: {e}')

    def write():
        try:
            with encode_lock:
                data = json.load(open(list_path, encoding='utf-8'))
                chapters = [{**c, 'start': cz.parse_ts(c['start'])} for c in data['chapters']]
                set_state(phase='writing', progress=0.0, message='Writing the audio file')
                n = cz.write_output(src, chapters, out, fmt, bitrate, total, work,
                                    progress=lambda p: state.update(progress=p, message='Encoding the audio (once per book)'))
            set_state(phase='done', progress=1.0, message=f'Wrote {n} chapters', out=out)
        except Exception as e:  # noqa: BLE001 - reported to the page
            set_state(phase='error', message=str(e))

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, body, ctype='application/json; charset=utf-8'):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False).encode('utf-8')
            elif isinstance(body, str):
                body = body.encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
            try:
                if u.path == '/':
                    return self.send(200, open(page_path, encoding='utf-8').read(), 'text/html; charset=utf-8')
                if u.path == '/api/book':
                    data = json.load(open(list_path, encoding='utf-8'))
                    chapters = [{**c, 'start': cz.parse_ts(c['start'])} for c in data['chapters']]
                    return self.send(200, {
                        'title': tags.get('album') or tags.get('title') or os.path.splitext(os.path.basename(src))[0],
                        'author': tags.get('artist') or tags.get('album_artist') or '',
                        'source': os.path.basename(src), 'duration': total, 'language': data.get('language'),
                        'warnings': data.get('warnings', []), 'chapters': chapters, 'format': fmt,
                        'out': os.path.basename(out), 'searchable': bool(segments)})
                if u.path == '/api/wave':
                    t = max(0.0, float(q['t']))
                    span = min(60.0, float(q.get('span', 16)))
                    return self.send(200, wave(src, t, span))
                if u.path == '/api/clip':
                    t = max(0.0, float(q['t']))
                    d = min(180.0, float(q.get('d', 20)))
                    return self.send(200, clip(src, t, d), 'audio/wav')
                if u.path == '/api/search':
                    words = q.get('q', '').lower().split()
                    hits = [{'t': t, 'text': text} for t, text in segments
                            if words and all(w in text.lower() for w in words)][:60]
                    return self.send(200, hits)
                if u.path == '/api/status':
                    return self.send(200, {**state, 'out': os.path.basename(state['out']),
                                           'folder': os.path.dirname(state['out'])})
                return self.send(404, {'error': 'not found'})
            except (KeyError, ValueError) as e:
                return self.send(400, {'error': str(e)})

        def do_POST(self):
            u = urllib.parse.urlparse(self.path)
            body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
            port = self.server.server_port
            if self.headers.get('Origin') not in (None, f'http://127.0.0.1:{port}', f'http://localhost:{port}'):
                return self.send(403, {'error': 'forbidden'})
            try:
                if u.path == '/api/chapters':
                    incoming = json.loads(body)['chapters']
                    chapters = []
                    for c in incoming:
                        start = float(c['start'])
                        if not (0 <= start <= total) or not str(c.get('title', '')).strip():
                            return self.send(400, {'error': 'Each chapter needs a title and a time inside the book.'})
                        keep = {k: c[k] for k in ('check', 'also_heard', 'heard') if c.get(k)}
                        chapters.append({'start': start, 'title': str(c['title']).strip(), **keep,
                                         **({'ok': True} if c.get('ok') else {})})
                    with save_lock:
                        data = json.load(open(list_path, encoding='utf-8'))
                        cz.save_list(list_path, data.get('source', src), data.get('language'), total,
                                     data.get('warnings', []), chapters)
                    if state['phase'] == 'done':
                        set_state(phase='idle', message='')
                    return self.send(200, {'saved': True})
                if u.path == '/api/write':
                    if state['phase'] == 'writing':
                        return self.send(409, {'error': 'Already writing'})
                    set_state(phase='writing', progress=0.0, message='Waiting for the audio to be ready')
                    threading.Thread(target=write, daemon=True).start()
                    return self.send(202, {'started': True})
                return self.send(404, {'error': 'not found'})
            except (KeyError, ValueError, TypeError) as e:
                return self.send(400, {'error': str(e)})

    httpd = http.server.ThreadingHTTPServer(('127.0.0.1', port), Handler)
    url = f'http://127.0.0.1:{httpd.server_port}/'
    threading.Thread(target=prepare, daemon=True).start()
    print(f'\nReview page: {url}\nChanges are saved to {list_path} as you go. Press Ctrl+C here when you are done.')
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\nStopped.')


def probe_tags(src):
    r = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format_tags', '-of', 'json', src],
                       capture_output=True, text=True)
    try:
        return {k.lower(): v for k, v in json.loads(r.stdout)['format'].get('tags', {}).items()}
    except (ValueError, KeyError):
        return {}


def pcm(src, t, d, rate):
    return subprocess.run(['ffmpeg', '-v', 'error', '-ss', f'{t:.3f}', '-t', f'{d:.3f}', '-i', src,
                           '-ac', '1', '-ar', str(rate), '-f', 's16le', '-'], capture_output=True, check=True).stdout


def wave(src, t, span):
    """Loudness in dB per 20 ms between t and t + span, decoded exactly from the source."""
    rate, step = 8000, 160
    raw = pcm(src, t, span, rate)
    n = len(raw) // 2
    samples = struct.unpack(f'<{n}h', raw[:n * 2])
    db = []
    for i in range(0, n - step + 1, step):
        chunk = samples[i:i + step]
        rms = math.sqrt(sum(x * x for x in chunk) / step) / 32768
        db.append(round(20 * math.log10(max(rms, 1e-6)), 1))
    return {'t0': t, 'step': step / rate, 'db': db}


def clip(src, t, d):
    """A WAV clip that starts exactly at t, so playback position is never a guess."""
    return subprocess.run(['ffmpeg', '-v', 'error', '-ss', f'{t:.3f}', '-t', f'{d:.3f}', '-i', src,
                           '-ac', '1', '-ar', '22050', '-f', 'wav', '-'], capture_output=True, check=True).stdout


if __name__ == '__main__':
    sys.exit('Start the review page with: chapterize.py BOOK --review')
