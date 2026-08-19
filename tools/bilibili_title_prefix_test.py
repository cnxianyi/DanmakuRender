"""Preview or apply a prefix to Bilibili archive titles.

The script lists all public videos for the configured Bilibili account.  Titles
starting with ``[`` are changed to::

    【三角洲行动】[original title]

Preview is the default.  Pass ``--apply`` to submit the title changes.

Examples:
    python tools/bilibili_title_prefix_test.py
    python tools/bilibili_title_prefix_test.py --apply
    python tools/bilibili_title_prefix_test.py --mid 123456 --apply
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from copy import deepcopy
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / 'configs' / 'global.yml'
DEFAULT_CONFIG_BASE = PROJECT_ROOT / 'DMR' / 'Config' / 'default.yml'
DEFAULT_PREFIX = '【三角洲行动】'
TITLE_LIMIT = 80

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _merge_dict(base, override):
    result = deepcopy(base or {})
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge_dict(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _load_upload_config(config_path: Path) -> dict:
    """Load the Bilibili uploader settings, including project defaults."""
    try:
        import yaml
    except ImportError as error:
        raise RuntimeError('读取 YAML 配置需要安装 PyYAML') from error

    with DEFAULT_CONFIG_BASE.open('r', encoding='utf-8') as file:
        default_config = yaml.safe_load(file) or {}
    with config_path.open('r', encoding='utf-8') as file:
        user_config = yaml.safe_load(file) or {}

    config = _merge_dict(default_config, user_config)
    upload_config = config.get('upload_args', {}).get('bilibili')
    if not isinstance(upload_config, dict):
        raise ValueError('全局配置中不存在 upload_args.bilibili')
    return upload_config


def _resolve_path(value, config_path: Path) -> Path | None:
    if not value:
        return None
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = (config_path.parent / path).resolve()
    return path


def _cookie_dict(cookie_path: Path | None) -> dict:
    """Read cookies saved by biliup-rs or a plain cookie mapping."""
    if cookie_path is None:
        return {}
    with cookie_path.open('r', encoding='utf-8') as file:
        payload = json.load(file)

    cookie_info = payload.get('cookie_info') if isinstance(payload, dict) else None
    if isinstance(cookie_info, dict):
        cookies = cookie_info.get('cookies') or []
        return {
            item['name']: item['value']
            for item in cookies
            if isinstance(item, dict) and item.get('name')
        }
    if isinstance(payload, dict) and isinstance(payload.get('cookies'), dict):
        return payload['cookies']
    if isinstance(payload, dict):
        return {
            str(key): str(value)
            for key, value in payload.items()
            if isinstance(value, (str, int, float))
        }
    return {}


def _current_mid(cookies: dict) -> str:
    try:
        import requests
    except ImportError as error:
        raise RuntimeError('获取 B 站用户信息需要安装 requests') from error

    response = requests.get(
        'https://api.bilibili.com/x/web-interface/nav',
        headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.bilibili.com/'},
        cookies=cookies,
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get('code') != 0 or not payload.get('data', {}).get('mid'):
        raise RuntimeError(f'无法从 cookies 获取当前 B 站用户 mid: {payload}')
    return str(payload['data']['mid'])


def _should_update(title: str) -> bool:
    return str(title or '').startswith('[')


def _prefixed_title(title: str, prefix: str = DEFAULT_PREFIX) -> str:
    title = str(title or '')
    if not _should_update(title):
        return title
    return (prefix + title)[:TITLE_LIMIT]


def _iter_videos(video_api, mid: str, page_size: int, max_pages: int = 0):
    page = 1
    seen_bvids = set()
    while not max_pages or page <= max_pages:
        data = video_api.fetch_user_videos(mid, page=page, page_size=page_size) or {}
        video_list = (data.get('list') or {}).get('vlist') or []
        if not video_list:
            break

        for video in video_list:
            bvid = str(video.get('bvid') or '').strip()
            if bvid and bvid not in seen_bvids:
                seen_bvids.add(bvid)
                yield bvid, str(video.get('title') or '')

        page_info = data.get('page') or {}
        total = int(page_info.get('count') or 0)
        if len(video_list) < page_size or (total and page * page_size >= total):
            break
        page += 1


def _parse_args():
    parser = argparse.ArgumentParser(
        description='查找 B 站标题以 [ 开头的稿件，并可添加【三角洲行动】前缀。',
    )
    parser.add_argument(
        '--config',
        type=Path,
        default=DEFAULT_CONFIG,
        help=f'全局配置路径，默认: {DEFAULT_CONFIG}',
    )
    parser.add_argument(
        '--cookies',
        type=Path,
        help='覆盖 upload_args.bilibili.cookies；不填写时使用配置值',
    )
    parser.add_argument(
        '--mid',
        help='B 站用户 mid；不填写时从 cookies 的当前登录用户获取',
    )
    parser.add_argument(
        '--prefix',
        default=DEFAULT_PREFIX,
        help=f'要添加的前缀，默认: {DEFAULT_PREFIX}',
    )
    parser.add_argument(
        '--page-size',
        type=int,
        default=30,
        help='每页视频数，默认 30',
    )
    parser.add_argument(
        '--max-pages',
        type=int,
        default=0,
        help='最多读取页数，0 表示全部读取',
    )
    parser.add_argument(
        '--pause',
        type=float,
        default=0.5,
        help='修改稿件之间的等待秒数，默认 0.5',
    )
    parser.add_argument(
        '--apply',
        action='store_true',
        help='真正提交标题修改；默认仅预览，不修改 B 站',
    )
    return parser.parse_args()


def main() -> int:
    cli_args = _parse_args()
    config_path = cli_args.config.expanduser().resolve()
    if not config_path.is_file():
        print(f'配置文件不存在: {config_path}', file=sys.stderr)
        return 1
    if cli_args.page_size <= 0 or cli_args.max_pages < 0 or cli_args.pause < 0:
        print('--page-size 必须大于 0，--max-pages 和 --pause 不能小于 0', file=sys.stderr)
        return 1

    try:
        upload_config = _load_upload_config(config_path)
        cookie_path = _resolve_path(
            cli_args.cookies or upload_config.get('cookies'),
            config_path,
        )
        cookies = _cookie_dict(cookie_path) if cookie_path else {}
        mid = str(cli_args.mid or _current_mid(cookies))

        # Import after argument/config validation so --help and configuration errors
        # do not initialize the full uploader stack.
        from DMR.LiveAPI.bilivideo import BiliVideoAPI

        video_api = BiliVideoAPI(cookies=cookies)
        candidates = []
        total_videos = 0
        for bvid, title in _iter_videos(
            video_api,
            mid,
            cli_args.page_size,
            cli_args.max_pages,
        ):
            total_videos += 1
            if _should_update(title):
                candidates.append((bvid, title, _prefixed_title(title, cli_args.prefix)))

        print(f'用户 mid={mid} 共读取 {total_videos} 个视频，找到 {len(candidates)} 个标题以 [ 开头的稿件。')
        if not candidates:
            return 0

        for bvid, old_title, new_title in candidates:
            print(f'{bvid}: {old_title} -> {new_title}')

        if not cli_args.apply:
            print('当前为预览模式；如需提交修改，请追加 --apply。')
            return 0

        if not cookie_path or not cookie_path.is_file():
            raise FileNotFoundError(f'提交修改需要有效的 cookies 文件: {cookie_path}')

        from DMR.Uploader.biliwebapi import BiliWebApi

        uploader = BiliWebApi(
            cookies=str(cookie_path),
            account=upload_config.get('account'),
            limit=upload_config.get('limit', 3),
        )
        try:
            changed = 0
            failed = 0
            for bvid, old_title, expected_title in candidates:
                try:
                    videos = uploader.get_remote_data(bvid)
                    if videos is None:
                        raise RuntimeError('无法读取远程稿件')
                    # Re-check the current title to avoid overwriting a title changed
                    # after the list request.
                    if not _should_update(videos.title):
                        print(f'{bvid}: 当前标题已不是 [ 开头，跳过。')
                        continue
                    new_title = _prefixed_title(videos.title, cli_args.prefix)
                    if videos.title == new_title:
                        print(f'{bvid}: 标题无需修改。')
                        continue
                    videos.title = new_title
                    uploader.submit(submit_api='web', videos=videos)
                    changed += 1
                    print(f'{bvid}: 修改成功。')
                    if cli_args.pause:
                        time.sleep(cli_args.pause)
                except Exception as error:
                    failed += 1
                    print(f'{bvid}: 修改失败: {error}', file=sys.stderr)
            print(f'处理完成：成功 {changed}，失败 {failed}，跳过 {len(candidates) - changed - failed}。')
            return 1 if failed else 0
        finally:
            uploader.stop()
    except Exception as error:
        print(f'测试失败: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
