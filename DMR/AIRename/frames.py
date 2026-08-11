import os
import subprocess

from DMR.utils import FFprobe, ToolsList, get_tempfile


def _video_duration(video):
    # 录制器中的 duration 可能是墙上时间；优先用文件本身的实际时长。
    duration = FFprobe.get_duration(video.path)
    if duration <= 0:
        try:
            duration = float(video.duration)
        except (TypeError, ValueError):
            duration = -1
    if duration <= 0:
        raise ValueError(f'无法获取视频时长: {video.path}')
    return duration


def _frame_positions(config):
    positions = config.get('frame_positions', [0.33, 0.66, 0.99])
    if not isinstance(positions, (list, tuple)) or not positions:
        raise ValueError('frame_positions 必须是非空数组')
    result = []
    for position in positions:
        position = float(position)
        if not 0 < position <= 1:
            raise ValueError('frame_positions 中的值必须大于 0 且小于等于 1')
        result.append(position)
    return result


def extract_frames(video, config):
    if not os.path.isfile(video.path):
        raise FileNotFoundError(f'视频文件不存在: {video.path}')

    duration = _video_duration(video)
    image_format = str(config.get('image_format', 'jpg')).lower()
    if image_format not in ('jpg', 'jpeg', 'png', 'webp'):
        raise ValueError(f'不支持的截图格式: {image_format}')
    max_width = max(0, int(config.get('max_image_width', 1280)))
    timeout = max(1, float(config.get('screenshot_timeout', 30)))
    frame_paths = []
    try:
        for index, position in enumerate(_frame_positions(config), start=1):
            timestamp = duration * position
            frame_path = get_tempfile(prefix=f'ai-rename-{index}', suffix=image_format)
            frame_paths.append(frame_path)
            fallback_timestamp = max(0, min(timestamp, duration - 1.0))
            timestamps = [timestamp]
            if fallback_timestamp != timestamp:
                timestamps.append(fallback_timestamp)

            error = ''
            for candidate_timestamp in timestamps:
                if os.path.exists(frame_path):
                    os.remove(frame_path)
                command = [
                    ToolsList.get('ffmpeg'), '-y',
                    '-ss', f'{candidate_timestamp:.3f}',
                    '-i', video.path,
                    '-frames:v', '1',
                ]
                if max_width:
                    command.extend(['-vf', f'scale=min({max_width}\\,iw):-2'])
                if image_format in ('jpg', 'jpeg'):
                    command.extend(['-q:v', str(config.get('jpeg_quality', 3))])
                command.append(frame_path)
                proc = subprocess.run(
                    command,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    timeout=timeout,
                )
                if proc.returncode == 0 and os.path.exists(frame_path) and os.path.getsize(frame_path) > 0:
                    break
                error = proc.stderr.decode('utf-8', errors='replace')[-500:]
            else:
                raise RuntimeError(f'FFmpeg 在 {position:.0%} 处截图失败: {error}')
        return frame_paths
    except Exception:
        for frame_path in frame_paths:
            try:
                if os.path.exists(frame_path):
                    os.remove(frame_path)
            except OSError:
                pass
        raise
