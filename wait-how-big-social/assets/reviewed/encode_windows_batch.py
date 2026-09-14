#!/usr/bin/env python3
"""BOTS-120: finite, offline scene encoding with observable receipts."""
import argparse
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import sys
import threading
import time


class Stopped(RuntimeError):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def stop_requested(out):
    return os.environ.get('WHB_STOP') == '1' or (out / 'STOP').exists()


def validate_batch(scene_dirs, output_dir):
    """Capture source bytes before any external process; never follow escaping links."""
    if not 1 <= len(scene_dirs) <= 6:
        raise ValueError('Provide one to six explicit scene directories')
    raw_out = Path(output_dir)
    if raw_out.exists() or raw_out.is_symlink():
        raise ValueError('Use a fresh output directory; existing output is never overwritten')
    out = raw_out.resolve()
    batch, ids = [], set()
    for raw in scene_dirs:
        source = Path(raw).resolve(strict=True)
        if not source.is_dir() or out == source or source in out.parents:
            raise ValueError('Output must be outside every source directory')
        cid = source.name
        if not cid or cid in ids:
            raise ValueError('Duplicate or empty source directory name')
        ids.add(cid)
        manifest_path = (source / 'SCENE_MANIFEST.json').resolve(strict=True)
        if not manifest_path.is_relative_to(source):
            raise ValueError('Manifest escapes source')
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
        if not isinstance(manifest, dict):
            raise ValueError('Manifest must be an object')
        fps, counts = manifest.get('fps'), manifest.get('frame_counts')
        if type(fps) is not int or not 1 <= fps <= 60:
            raise ValueError('fps must be an integer between 1 and 60')
        if not isinstance(counts, list) or not 1 <= len(counts) <= 100:
            raise ValueError('Provide one to 100 frame counts')
        if any(type(n) is not int or n <= 0 for n in counts) or sum(counts) > 18000:
            raise ValueError('Frame counts must be positive integers totaling at most 18000')
        frames = []
        total_bytes = 0
        for i, count in enumerate(counts):
            path = (source / 'scenes' / f'scene-{i}.png').resolve(strict=True)
            if not path.is_relative_to(source) or not path.is_file():
                raise ValueError('Scene escapes source or is not a file')
            if path.stat().st_size > 32 * 1024 * 1024:
                raise ValueError('Scene exceeds 32 MiB')
            data = path.read_bytes()
            total_bytes += len(data)
            if total_bytes > 128 * 1024 * 1024 or not data.startswith(b'\x89PNG\r\n\x1a\n'):
                raise ValueError('Invalid PNG or excessive scene memory')
            frames.append({'path': str(path), 'sha256': digest(data), 'count': count, 'data': data})
        batch.append({'content_id': cid, 'source': str(source), 'fps': fps, 'counts': counts,
                      'manifest_sha256': digest(manifest_bytes), 'frames': frames})
    if sum(sum(len(f['data']) for f in item['frames']) for item in batch) > 256 * 1024 * 1024:
        raise ValueError('Batch exceeds 256 MiB source memory')
    return batch


def check_probe(probe, item):
    videos = [s for s in probe['streams'] if s.get('codec_type') == 'video']
    audios = [s for s in probe['streams'] if s.get('codec_type') == 'audio']
    expected = sum(item['counts'])
    if len(videos) != 1 or audios:
        raise ValueError('Expected exactly one video stream and zero audio streams')
    v = videos[0]
    duration = float(probe['format']['duration'])
    if (v['width'], v['height'], int(v['nb_read_frames'])) != (1080, 1920, expected):
        raise ValueError('Unexpected video dimensions or frame count')
    if not math.isfinite(duration) or abs(duration - expected / item['fps']) > 1 / item['fps']:
        raise ValueError('Unexpected video duration')
    if v.get('codec_name') != 'h264':
        raise ValueError('Expected H264 output')
    return {'frames': expected, 'duration_seconds': duration, 'width': 1080,
            'height': 1920, 'audio_streams': 0, 'codec': v['codec_name']}


def encode_item(item, out, ffmpeg, ffprobe, version, timeout=300):
    output = out / f"{item['content_id']}-silent-video.mp4"
    command = [ffmpeg, '-n', '-hide_banner', '-loglevel', 'error', '-f', 'image2pipe',
               '-framerate', str(item['fps']), '-c:v', 'png', '-i', 'pipe:0',
               '-frames:v', str(sum(item['counts'])), '-vf', 'scale=1080:1920:flags=lanczos,format=yuv420p',
               '-c:v', 'libx264', '-profile:v', 'high', '-level', '4.1', '-b:v', '2500000',
               '-maxrate', '2500000', '-bufsize', '5000000', '-an', '-movflags', '+faststart', str(output)]
    row = {'jira_key': 'BOTS-120', 'content_id': item['content_id'], 'host': platform.node(),
           'started_at_utc': utc(), 'source_dir': item['source'], 'manifest_sha256': item['manifest_sha256'],
           'source_inputs': [{k: f[k] for k in ('path', 'sha256', 'count')} for f in item['frames']],
           'command': command, 'encoder_version': version, 'output_file': str(output),
           'status': 'failed', 'model_calls': 0, 'publication': False}
    start, proc, finished = time.monotonic(), None, threading.Event()
    abort = []
    try:
        if stop_requested(out):
            raise Stopped('STOP before item')
        if output.exists() or output.is_symlink():
            raise ValueError('Output collision')
        stderr_path = out / f"{item['content_id']}-encoder.stderr.txt"
        with stderr_path.open('xb') as err:
            proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=err)
            def watchdog():
                while not finished.wait(0.1):
                    reason = 'stopped' if stop_requested(out) else 'timeout' if time.monotonic() - start > timeout else None
                    if reason:
                        abort.append(reason)
                        try:
                            proc.kill()
                        except OSError:
                            pass
                        return
            watcher = threading.Thread(target=watchdog, daemon=True)
            watcher.start()
            try:
                for frame in item['frames']:
                    for _ in range(frame['count']):
                        if stop_requested(out):
                            raise Stopped('STOP during encoding')
                        proc.stdin.write(frame['data'])
                proc.stdin.close()
                row['encoder_exit_code'] = proc.wait(timeout=timeout)
                if abort:
                    raise Stopped('STOP during encoding') if abort[0] == 'stopped' else TimeoutError('Encoder deadline exceeded')
                if row['encoder_exit_code'] != 0:
                    raise RuntimeError('Encoder failed; inspect stderr receipt')
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=10)
                finished.set()
                watcher.join(timeout=2)
        if stop_requested(out):
            raise Stopped('STOP before verification')
        probe_cmd = [ffprobe, '-v', 'error', '-count_frames', '-show_streams', '-show_format', '-of', 'json', str(output)]
        row['probe_command'] = probe_cmd
        probe = json.loads(subprocess.check_output(probe_cmd, text=True, timeout=60))
        row['probe'] = check_probe(probe, item)
        row['output_sha256'] = digest(output.read_bytes())
        row['output_bytes'] = output.stat().st_size
        if stop_requested(out):
            raise Stopped('STOP during verification')
        row['status'] = 'success'
    except Exception as exc:
        row['status'] = 'stopped' if isinstance(exc, Stopped) or 'stopped' in abort else 'failed'
        row['error'] = str(exc)
    finally:
        finished.set()
        row['ended_at_utc'] = utc()
        row['elapsed_seconds'] = round(time.monotonic() - start, 3)
        with (out / 'receipts.jsonl').open('a', encoding='utf-8') as f:
            f.write(json.dumps(row) + '\n')
    return row


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scene-dir', action='append', required=True)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--ffmpeg', required=True)
    parser.add_argument('--ffprobe', required=True)
    args = parser.parse_args(argv)
    out = Path(args.output_dir)
    if stop_requested(out):
        print('STOP engaged; no inputs or outputs changed', file=sys.stderr)
        return 3
    try:
        batch = validate_batch(args.scene_dir, out)
        ffmpeg, ffprobe = (str(Path(p).resolve(strict=True)) for p in (args.ffmpeg, args.ffprobe))
        for tool in (ffmpeg, ffprobe):
            if not Path(tool).is_file():
                raise ValueError('Explicit encoder/probe file required')
        version = subprocess.check_output([ffmpeg, '-version'], text=True, timeout=15).splitlines()[0]
        out.mkdir(parents=True, exist_ok=False)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f'Preflight failed: {exc}', file=sys.stderr)
        return 2
    for item in batch:
        row = encode_item(item, out, ffmpeg, ffprobe, version)
        print(json.dumps({'content_id': row['content_id'], 'status': row['status']}))
        if row['status'] != 'success':
            return 3 if row['status'] == 'stopped' else 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
