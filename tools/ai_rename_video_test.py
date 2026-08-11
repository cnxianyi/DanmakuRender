"""Run the AI game recognizer against one local video.

Usage:
    python tools/ai_rename_video_test.py "D:\\videos\\record.flv"
    python tools/ai_rename_video_test.py "D:\\videos\\record.flv" --keep-frames
"""

import argparse
import json
import os
import sys
from copy import deepcopy
from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from DMR.AIRename.client import OpenAICompatibleVisionClient
from DMR.AIRename.frames import extract_frames
from DMR.AIRename.parser import fixed_game_result, parse_game_result
from DMR.utils import VideoInfo


DEFAULT_CONFIG = PROJECT_ROOT / 'configs' / 'global.yml'
DEFAULT_CONFIG_BASE = PROJECT_ROOT / 'DMR' / 'Config' / 'default.yml'


def _merge_dict(base, override):
    result = deepcopy(base or {})
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge_dict(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _load_ai_config(config_path):
    with DEFAULT_CONFIG_BASE.open('r', encoding='utf-8') as file:
        default_config = yaml.safe_load(file) or {}
    with config_path.open('r', encoding='utf-8') as file:
        user_config = yaml.safe_load(file) or {}

    return _merge_dict(
        default_config.get('ai_rename_args', {}),
        user_config.get('ai_rename_args', {}),
    )


def _parse_args():
    parser = argparse.ArgumentParser(
        description='在视频的配置位置截图，并使用当前 AI 配置识别游戏。',
    )
    parser.add_argument('video', help='需要测试的视频文件路径')
    parser.add_argument(
        '--config',
        type=Path,
        default=DEFAULT_CONFIG,
        help=f'全局配置文件路径，默认: {DEFAULT_CONFIG}',
    )
    parser.add_argument(
        '--keep-frames',
        action='store_true',
        help='测试结束后保留 FFmpeg 截图，并在标准错误中显示路径',
    )
    return parser.parse_args()


def main():
    cli_args = _parse_args()
    video_path = Path(cli_args.video).expanduser().resolve()
    config_path = cli_args.config.expanduser().resolve()
    frame_paths = []

    try:
        if not video_path.is_file():
            raise FileNotFoundError(f'视频文件不存在: {video_path}')
        if not config_path.is_file():
            raise FileNotFoundError(f'配置文件不存在: {config_path}')

        os.chdir(PROJECT_ROOT)
        ai_config = _load_ai_config(config_path)
        result = fixed_game_result(ai_config)
        if result is None:
            frame_paths = extract_frames(VideoInfo(path=str(video_path)), ai_config)
            for index, frame_path in enumerate(frame_paths, start=1):
                print(f'截图 {index}: {Path(frame_path).resolve()}', file=sys.stderr)

            response_text = OpenAICompatibleVisionClient(ai_config).recognize(frame_paths)
            result = parse_game_result(response_text, ai_config)
        else:
            print(f'使用固定游戏名，不截图、不请求 AI: {result["main_game"]}', file=sys.stderr)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as error:
        print(f'测试失败: {error}', file=sys.stderr)
        return 1
    finally:
        if not cli_args.keep_frames:
            for frame_path in frame_paths:
                try:
                    if os.path.exists(frame_path):
                        os.remove(frame_path)
                except OSError as error:
                    print(f'无法删除临时截图 {frame_path}: {error}', file=sys.stderr)


if __name__ == '__main__':
    raise SystemExit(main())
