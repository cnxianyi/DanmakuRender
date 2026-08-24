import logging
import os
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Tuple

from DMR.utils import PipeMessage, uuid

from .parser import normalize_game
from .screenshots import extract_first_frame
from .telegram import (
    get_updates,
    parse_update_reply,
    send_frame_album,
    telegram_config,
    telegram_enabled,
)


class AIRename():
    def __init__(
        self,
        pipe: Tuple[queue.Queue, queue.Queue],
        nworkers: int = 1,
        **kwargs,
    ) -> None:
        self.send_queue, self.recv_queue = pipe
        self.logger = logging.getLogger(__name__)
        self.stoped = True
        self._piperecvprocess = None
        self._executors = ThreadPoolExecutor(max_workers=max(1, int(nworkers)))
        self._tasks = {}
        self._lock = threading.Lock()
        self._telegram_targets = {}
        self._telegram_monitors = {}

    def _pipeSend(self, event, msg, target='engine', request_id=None, data=None):
        if self.send_queue:
            self.send_queue.put(PipeMessage(
                source='ai_rename',
                target=target,
                event=event,
                request_id=request_id,
                msg=msg,
                dtype='dict',
                data=data,
            ))

    def _pipeRecvMonitor(self):
        while not self.stoped and self.recv_queue is not None:
            message: PipeMessage = self.recv_queue.get()
            try:
                if message.target == 'ai_rename' and message.event == 'newtask':
                    self.add_task(message)
            except Exception as e:
                self.logger.error(
                    f'Game name message {message.event} ({message.request_id}) raised an error.'
                )
                self.logger.exception(e)

    def start(self):
        self.stoped = False
        self._piperecvprocess = threading.Thread(target=self._pipeRecvMonitor, daemon=True)
        self._piperecvprocess.start()

    def add_task(self, msg: PipeMessage):
        data = msg.data or {}
        videos = list(data.get('videos') or [])
        if not videos and data.get('video') is not None:
            videos = [data['video']]
        if not videos:
            raise ValueError('Telegram 截图任务没有视频')
        task = {
            'uuid': uuid(),
            'source': msg.source,
            'request_id': msg.request_id,
            'videos': videos,
            'group_id': data.get('group_id') or videos[0].group_id,
            'args': data.get('args', {}),
        }
        with self._lock:
            self._tasks[task['uuid']] = task
        self._executors.submit(self._process_screenshots, task)

    def _process_screenshots(self, task):
        videos = task['videos']
        args = task['args']
        result = {
            'group_id': task['group_id'],
            'screenshot_count': 0,
            'failed_segments': [],
        }
        frame_paths = []
        try:
            for video in videos:
                try:
                    frame_paths.append((video, extract_first_frame(video, args)))
                except Exception as error:
                    result['failed_segments'].append(video.segment_id)
                    self.logger.warning(f'视频 {video.path} 首帧截图失败，已跳过: {error}')

            if frame_paths:
                result['screenshot_count'] = len(frame_paths)
                self._send_telegram_album(task, frame_paths, result)
            else:
                self.logger.warning(f'直播组 {task["group_id"]} 没有可发送的 Telegram 截图.')
            self._pipeSend(
                event='screenshots_end',
                msg=f'直播组 {task["group_id"]} Telegram 截图处理完成: {result["screenshot_count"]} 张',
                target=task['source'],
                request_id=task['request_id'],
                data=result,
            )
        except Exception as e:
            result['error'] = str(e)
            self.logger.warning(f'直播组 {task["group_id"]} Telegram 截图处理失败: {e}')
            self._pipeSend(
                event='screenshots_error',
                msg=f'直播组 {task["group_id"]} Telegram 截图处理失败: {e}',
                target=task['source'],
                request_id=task['request_id'],
                data=result,
            )
        finally:
            for _video, frame_path in frame_paths:
                try:
                    if os.path.exists(frame_path):
                        os.remove(frame_path)
                except OSError:
                    self.logger.debug(f'无法删除 Telegram 截图临时文件: {frame_path}')
            with self._lock:
                self._tasks.pop(task['uuid'], None)

    def _caption(self, task, videos, args):
        tg_config = telegram_config(args)
        fixed_game = args.get('game', '') if args.get('fixed_game') else ''
        segment_names = ', '.join(
            str(video.segment_id) if video.segment_id is not None else '?'
            for video in videos
        )
        game_line = f'当前固定游戏名：{fixed_game}\n' if fixed_game else '当前固定游戏名：未设置\n'
        update_hint = (
            '回复本相册中的任意图片并直接发送游戏名'
            if args.get('update_bv_title') else ''
        )
        values = {
            'TASKNAME': videos[0].taskname or task['source'].split('/', 1)[-1],
            'COUNT': len(videos),
            'SEGMENTS': segment_names,
            'GROUP_ID': task['group_id'],
            'GAME': fixed_game or '未设置游戏名',
            'GAME_LINE': game_line.rstrip('\n'),
            'UPDATE_HINT': update_hint,
            # Keep the old placeholders valid for task-level custom captions.
            'SEGMENT_ID': '',
            'BASENAME': os.path.basename(videos[0].path),
        }
        template = tg_config.get(
            'caption',
            '{TASKNAME} | 直播结束 | {COUNT} 个视频\n'
            '截图顺序：{SEGMENTS}\n{GAME_LINE}\n{UPDATE_HINT}',
        )
        try:
            return str(template).format(**values).strip()
        except (KeyError, ValueError):
            return (
                f'{values["TASKNAME"]} | 直播结束 | {len(videos)} 个视频\n'
                f'截图顺序：{segment_names}\n{game_line}{update_hint}'
            ).strip()

    def _send_telegram_album(self, task, frame_items, result):
        args = task['args']
        if not telegram_enabled(args):
            return
        frame_paths = [frame_path for _video, frame_path in frame_items]
        try:
            album = send_frame_album(
                frame_paths,
                args,
                caption=self._caption(task, [video for video, _path in frame_items], args),
            )
            result['telegram'] = album
            self._register_telegram_album(task, album)
            self.logger.info(
                f'直播组 {task["group_id"]} 的 {len(frame_paths)} 张首帧截图已发送到 Telegram.'
            )
        except Exception as error:
            # Notifications must never block recording, rendering or uploading.
            result['telegram_error'] = str(error)
            self.logger.warning(f'直播组 {task["group_id"]} 的 Telegram 截图发送失败: {error}')

    def _register_telegram_album(self, task, album):
        if not album or not album.get('message_ids') or not task['args'].get('update_bv_title'):
            return
        args = task['args']
        tg_config = telegram_config(args)
        token = str(tg_config.get('bot_token') or tg_config.get('token') or '').strip()
        api_base = str(tg_config.get('api_base') or 'https://api.telegram.org').rstrip('/')
        monitor_key = (token, api_base)
        reply_window = max(0, float(tg_config.get('reply_window', 86400)))
        if not reply_window:
            return
        target = {
            'source': task['source'],
            'request_id': task['request_id'],
            'group_id': task['group_id'],
            'game_config': {
                'max_game_name_length': args.get('max_game_name_length', 10),
                'excluded_names': args.get('excluded_names', []),
            },
            'expires_at': time.time() + reply_window,
        }
        with self._lock:
            for message_id in album['message_ids']:
                self._telegram_targets[(monitor_key, str(album['chat_id']), message_id)] = target
            monitor = self._telegram_monitors.get(monitor_key)
            if monitor is None or not monitor.is_alive():
                monitor = threading.Thread(
                    target=self._telegram_update_monitor,
                    args=(monitor_key, args),
                    daemon=True,
                )
                self._telegram_monitors[monitor_key] = monitor
                monitor.start()

    def _telegram_update_monitor(self, monitor_key, args):
        offset = None
        tg_config = telegram_config(args)
        retry_interval = max(1, float(tg_config.get('poll_retry_interval', 5)))
        while not self.stoped:
            try:
                updates = get_updates(args, offset=offset)
                for update in updates:
                    update_id = update.get('update_id')
                    if update_id is not None:
                        offset = max(offset or 0, int(update_id) + 1)
                    command = parse_update_reply(update)
                    if not command:
                        continue
                    target_key = (
                        monitor_key,
                        command['chat_id'],
                        command['reply_message_id'],
                    )
                    with self._lock:
                        target = self._telegram_targets.get(target_key)
                    if not target or target['expires_at'] < time.time():
                        continue
                    game = normalize_game(command['game'], target['game_config'])
                    if not game:
                        self.logger.warning('Telegram 回复的游戏名为空、过长或已被过滤，已忽略.')
                        continue
                    self._pipeSend(
                        event='telegram_update',
                        msg=f'Telegram 人工指定游戏名: {game}',
                        target=target['source'],
                        request_id=target['request_id'],
                        data={
                            'group_id': target['group_id'],
                            'game': game,
                            'chat_id': command['chat_id'],
                            'reply_message_id': command['reply_message_id'],
                        },
                    )
                self._prune_telegram_targets(monitor_key)
            except Exception as error:
                token = monitor_key[0]
                safe_error = str(error).replace(token, '***') if token else str(error)
                self.logger.warning(f'Telegram 回复轮询失败，{retry_interval:g} 秒后重试: {safe_error}')
                time.sleep(retry_interval)

    def _prune_telegram_targets(self, monitor_key):
        now = time.time()
        with self._lock:
            self._telegram_targets = {
                key: target
                for key, target in self._telegram_targets.items()
                if key[0] != monitor_key or target['expires_at'] >= now
            }

    def stop(self):
        self.stoped = True
        self._executors.shutdown(wait=False)
        self.logger.info('Game name processor stopped.')
