import json
import os
import re
import subprocess
from dataclasses import dataclass

from DMR.utils import ToolsList


_CLIP_COMMAND_PATTERN = re.compile(r'^\s*[Pp]\s*(\d+)\s+(.+?)\s*$', re.S)


@dataclass(frozen=True)
class ClipCommand:
    part_number: int
    remove_ranges: tuple[tuple[float | None, float | None], ...]


def _parse_time(value):
    value = value.strip()
    if not value:
        return None
    if not re.fullmatch(r'\d+(?::\d+){0,2}(?:\.\d+)?', value):
        raise ValueError(f'无效时间“{value}”；支持 SS、MM:SS 或 HH:MM:SS.mmm')

    fields = value.split(':')
    numbers = [float(field) for field in fields]
    if len(numbers) == 1:
        seconds = numbers[0]
    elif len(numbers) == 2:
        minutes, second = numbers
        if second >= 60:
            raise ValueError(f'无效时间“{value}”：秒必须小于 60')
        seconds = minutes * 60 + second
    else:
        hours, minutes, second = numbers
        if minutes >= 60 or second >= 60:
            raise ValueError(f'无效时间“{value}”：分和秒必须小于 60')
        seconds = hours * 3600 + minutes * 60 + second
    return seconds


class _RangeParser:
    def __init__(self, value):
        self.value = value
        self.pos = 0

    def parse(self):
        self._space()
        if self._peek() != '[':
            raise ValueError('P号后必须包含剪除区间 []')
        self._consume('[')
        self._space()
        if self._peek() == '[':
            ranges = [self._range()]
            self._space()
            while self._peek() == ',':
                self._consume(',')
                ranges.append(self._range())
                self._space()
            self._consume(']')
        else:
            ranges = [self._range_contents()]
        self._space()
        if self.pos != len(self.value):
            raise ValueError(f'剪除区间后存在无法识别的内容：{self.value[self.pos:]}')
        return ranges

    def _range(self):
        self._space()
        self._consume('[')
        return self._range_contents()

    def _range_contents(self):
        start = self._time_until(',')
        self._consume(',')
        end = self._time_until(']')
        self._consume(']')
        return _parse_time(start), _parse_time(end)

    def _time_until(self, delimiter):
        self._space()
        start = self.pos
        while self.pos < len(self.value) and self.value[self.pos] != delimiter:
            if self.value[self.pos] in '[],' and self.value[self.pos] != delimiter:
                break
            self.pos += 1
        if self.pos >= len(self.value) or self.value[self.pos] != delimiter:
            raise ValueError(f'剪除区间缺少“{delimiter}”')
        return self.value[start:self.pos].strip()

    def _space(self):
        while self.pos < len(self.value) and self.value[self.pos].isspace():
            self.pos += 1

    def _peek(self):
        return self.value[self.pos] if self.pos < len(self.value) else ''

    def _consume(self, expected):
        self._space()
        if self._peek() != expected:
            actual = self._peek() or '结尾'
            raise ValueError(f'剪除区间应为“{expected}”，实际为“{actual}”')
        self.pos += 1
        self._space()


def parse_clip_command(text):
    """Parse `P3 [03:00,45:00]` and return None for non-clip text."""
    match = _CLIP_COMMAND_PATTERN.fullmatch(str(text or ''))
    if not match:
        return None
    part_number = int(match.group(1))
    if part_number < 1:
        raise ValueError('P号必须从 P1 开始')
    ranges = _RangeParser(match.group(2)).parse()
    if not ranges:
        raise ValueError('至少需要一个剪除区间')
    return ClipCommand(part_number, tuple(ranges))


def normalize_remove_ranges(remove_ranges, duration):
    """Resolve open endpoints, validate bounds and merge overlapping ranges."""
    duration = float(duration)
    if duration <= 0:
        raise ValueError('无法获取视频时长')

    normalized = []
    for raw_start, raw_end in remove_ranges:
        start = 0.0 if raw_start is None else float(raw_start)
        end = duration if raw_end is None else float(raw_end)
        if start < 0 or end < 0:
            raise ValueError('剪除时间不能为负数')
        if start >= duration:
            raise ValueError(f'剪除起点 {format_time(start)} 超出视频时长 {format_time(duration)}')
        if end > duration:
            raise ValueError(f'剪除终点 {format_time(end)} 超出视频时长 {format_time(duration)}')
        if start >= end:
            raise ValueError(
                f'剪除区间起点必须早于终点：[{format_time(start)},{format_time(end)}]'
            )
        normalized.append((start, end))

    normalized.sort()
    merged = []
    for start, end in normalized:
        if merged and start <= merged[-1][1] + 0.001:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def kept_ranges(remove_ranges, duration):
    cuts = normalize_remove_ranges(remove_ranges, duration)
    keep = []
    cursor = 0.0
    for start, end in cuts:
        if start > cursor + 0.001:
            keep.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration - 0.001:
        keep.append((cursor, duration))
    if not keep or sum(end - start for start, end in keep) < 0.1:
        raise ValueError('剪除区间覆盖了整个视频，已拒绝执行')
    return cuts, keep


def format_time(seconds):
    total_milliseconds = int(round(max(0.0, float(seconds)) * 1000))
    hours, remainder = divmod(total_milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    second, milliseconds = divmod(remainder, 1000)
    if milliseconds == 0:
        return f'{hours:02d}:{minutes:02d}:{second:02d}'
    return f'{hours:02d}:{minutes:02d}:{second:02d}.{milliseconds:03d}'


def format_ranges(ranges):
    return ', '.join(f'[{format_time(start)},{format_time(end)}]' for start, end in ranges)


def resolve_local_part(part_title, candidate_paths):
    expected = str(part_title or '').strip()
    matches = []
    for path in candidate_paths or []:
        path = str(path or '')
        if not path or not os.path.isfile(path):
            continue
        stem = os.path.splitext(os.path.basename(path))[0]
        if stem == expected:
            matches.append(os.path.abspath(path))
    if not matches:
        raise FileNotFoundError(f'找不到 B 站分P“{expected}”对应的本地视频')
    if len(matches) > 1:
        raise RuntimeError(f'分P“{expected}”匹配到多个本地视频，已拒绝执行')
    return matches[0]


def probe_media(path):
    proc = subprocess.run(
        [
            ToolsList.get('ffprobe'), '-v', 'error', '-print_format', 'json',
            '-show_format', '-show_streams', path,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f'ffprobe 检查视频失败：{proc.stderr[-500:]}')
    data = json.loads(proc.stdout)
    duration = float((data.get('format') or {}).get('duration') or 0)
    streams = data.get('streams') or []
    if not any(stream.get('codec_type') == 'video' for stream in streams):
        raise ValueError('目标文件没有视频轨道')
    return {
        'duration': duration,
        'has_audio': any(stream.get('codec_type') == 'audio' for stream in streams),
    }


def build_cut_command(input_path, output_path, keep, has_audio, render_args=None):
    render_args = render_args or {}
    ffmpeg = ToolsList.get('ffmpeg')
    command = [ffmpeg, '-y']
    command.extend(str(value) for value in (render_args.get('hwaccel_args') or []))
    command.extend(['-i', input_path])

    filters = []
    concat_inputs = []
    for index, (start, end) in enumerate(keep):
        filters.append(
            f'[0:v:0]trim=start={start:.6f}:end={end:.6f},'
            f'setpts=PTS-STARTPTS[v{index}]'
        )
        concat_inputs.append(f'[v{index}]')
        if has_audio:
            filters.append(
                f'[0:a:0]atrim=start={start:.6f}:end={end:.6f},'
                f'asetpts=PTS-STARTPTS[a{index}]'
            )
            concat_inputs.append(f'[a{index}]')

    if len(keep) > 1:
        filters.append(
            ''.join(concat_inputs)
            + f'concat=n={len(keep)}:v=1:a={1 if has_audio else 0}'
            + ('[vcat][acat]' if has_audio else '[vcat]')
        )
        video_label = 'vcat'
        audio_label = 'acat'
    else:
        video_label = 'v0'
        audio_label = 'a0'

    vencoder = str(render_args.get('vencoder') or 'libx264')
    if 'vaapi' in vencoder:
        filters.append(f'[{video_label}]format=nv12,hwupload[vout]')
        video_label = 'vout'

    command.extend(['-filter_complex', ';'.join(filters), '-map', f'[{video_label}]'])
    if has_audio:
        command.extend(['-map', f'[{audio_label}]'])
    command.extend(['-c:v', vencoder])
    command.extend(str(value) for value in (render_args.get('vencoder_args') or []))
    if has_audio:
        command.extend(['-c:a', str(render_args.get('aencoder') or 'aac')])
        command.extend(str(value) for value in (render_args.get('aencoder_args') or []))
    command.extend(['-movflags', '+faststart', output_path])
    return command


def cut_video(input_path, output_path, remove_ranges, render_args=None):
    media = probe_media(input_path)
    cuts, keep = kept_ranges(remove_ranges, media['duration'])
    expected_duration = sum(end - start for start, end in keep)
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    command = build_cut_command(
        os.path.abspath(input_path),
        os.path.abspath(output_path),
        keep,
        media['has_audio'],
        render_args,
    )
    proc = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        try:
            os.remove(output_path)
        except OSError:
            pass
        raise RuntimeError(f'FFmpeg 剪辑失败（返回码 {proc.returncode}）：{proc.stdout[-1000:]}')

    output_media = probe_media(output_path)
    tolerance = max(1.0, expected_duration * 0.002)
    if abs(output_media['duration'] - expected_duration) > tolerance:
        raise RuntimeError(
            f'剪辑结果时长异常：预期 {format_time(expected_duration)}，'
            f'实际 {format_time(output_media["duration"])}'
        )
    return {
        'path': os.path.abspath(output_path),
        'source_duration': media['duration'],
        'duration': output_media['duration'],
        'removed_ranges': cuts,
        'kept_ranges': keep,
        'command': command,
    }
