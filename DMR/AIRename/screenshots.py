import os
import subprocess

from DMR.utils import ToolsList, get_tempfile


def extract_first_frame(video, config=None):
    """Extract the first decoded video frame for human review."""
    config = config or {}
    if not os.path.isfile(video.path):
        raise FileNotFoundError(f'视频文件不存在: {video.path}')

    image_format = str(
        config.get('screenshot_image_format', config.get('image_format', 'jpg'))
    ).lower()
    if image_format not in ('jpg', 'jpeg', 'png', 'webp'):
        raise ValueError(f'不支持的截图格式: {image_format}')

    max_width = max(0, int(
        config.get('screenshot_max_width', config.get('max_image_width', 1280))
    ))
    timeout = max(1, float(config.get('screenshot_timeout', 30)))
    frame_path = get_tempfile(prefix='tg-screenshot', suffix=image_format)
    command = [
        ToolsList.get('ffmpeg'), '-y',
        '-i', video.path,
        '-map', '0:v:0',
        '-frames:v', '1',
        '-an',
    ]
    if max_width:
        command.extend(['-vf', f'scale=min({max_width}\\,iw):-2'])
    if image_format in ('jpg', 'jpeg'):
        command.extend(['-q:v', str(config.get('screenshot_jpeg_quality', 3))])
    command.append(frame_path)

    try:
        proc = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        if proc.returncode != 0 or not os.path.exists(frame_path) or os.path.getsize(frame_path) <= 0:
            error = proc.stderr.decode('utf-8', errors='replace')[-500:]
            raise RuntimeError(f'FFmpeg 首帧截图失败: {error}')
        return frame_path
    except Exception:
        try:
            if os.path.exists(frame_path):
                os.remove(frame_path)
        except OSError:
            pass
        raise


def extract_midpoint(video, config=None):
    """Backward-compatible alias for the first-frame screenshot behavior."""
    return extract_first_frame(video, config)
