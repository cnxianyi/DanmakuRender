import hashlib
import json
import os
import threading


class AIRenameCache():
    _lock = threading.Lock()

    def __init__(self, path):
        self.path = path

    def make_key(self, video, args):
        stat = os.stat(video.path)
        key_data = {
            'path': os.path.abspath(video.path),
            'size': stat.st_size,
            'mtime_ns': stat.st_mtime_ns,
            'model': args.get('model'),
            'prompt': args.get('prompt'),
            'frame_positions': args.get('frame_positions', [0.33, 0.66, 0.99]),
        }
        raw = json.dumps(key_data, ensure_ascii=False, sort_keys=True).encode('utf-8')
        return hashlib.sha256(raw).hexdigest()

    def _load(self):
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def get(self, key):
        with self._lock:
            return self._load().get(key)

    def set(self, key, value):
        with self._lock:
            data = self._load()
            data[key] = value
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            temp_path = self.path + '.tmp'
            with open(temp_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(temp_path, self.path)
