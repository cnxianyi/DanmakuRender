import logging
import os
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Tuple

from DMR.utils import PipeMessage, VideoInfo, uuid

from .cache import AIRenameCache
from .client import OpenAICompatibleVisionClient
from .frames import extract_frames
from .parser import normalize_game, parse_game_result
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
                    f'AI rename message {message.event} ({message.request_id}) raised an error.'
                )
                self.logger.exception(e)

    def start(self):
        self.stoped = False
        self._piperecvprocess = threading.Thread(target=self._pipeRecvMonitor, daemon=True)
        self._piperecvprocess.start()

    def add_task(self, msg: PipeMessage):
        video: VideoInfo = msg.data['video']
        task = {
            'uuid': uuid(),
            'source': msg.source,
            'request_id': msg.request_id,
            'video': video,
            'args': msg.data.get('args', {}),
        }
        with self._lock:
            self._tasks[task['uuid']] = task
        self._executors.submit(self._recognize, task)

    def _recognize(self, task):
        video: VideoInfo = task['video']
        args = task['args']
        result = {
            'group_id': video.group_id,
            'segment_id': video.segment_id,
            'games': [],
            'main_game': '',
        }
        frame_paths = []
        try:
            fixed_result = args.get('_fixed_result')
            if fixed_result is not None:
                frame_paths = extract_frames(video, args)
                result.update(fixed_result)
            else:
                cache = AIRenameCache(args.get('cache_file', '.temp/ai_rename_cache.json'))
                cache_key = cache.make_key(video, args)
                cached = cache.get(cache_key) if args.get('cache', True) else None
                # TG needs the actual files even when the AI result is already cached.
                if cached is not None and not telegram_enabled(args):
                    result.update(cached)
                else:
                    frame_paths = extract_frames(video, args)
                    if cached is not None:
                        result.update(cached)
                    else:
                        response_text = OpenAICompatibleVisionClient(args).recognize(frame_paths)
                        result.update(parse_game_result(response_text, args))
                    if cached is None and args.get('cache', True):
                        cache.set(cache_key, {
                            'games': result['games'],
                            'main_game': result['main_game'],
                        })

            self._send_telegram_frames(task, frame_paths, result)
            game = result['main_game'] or '未识别到游戏'
            action = '固定游戏截图处理完成' if fixed_result is not None else 'AI 识别完成'
            self._pipeSend(
                event='end',
                msg=f'视频 {video.path} {action}: {game}',
                target=task['source'],
                request_id=task['request_id'],
                data=result,
            )
        except Exception as e:
            result['error'] = str(e)
            if frame_paths:
                self._send_telegram_frames(task, frame_paths, result)
            self.logger.warning(f'视频 {video.path} AI/TG 处理失败，将保留原文件名: {e}')
            self._pipeSend(
                event='error',
                msg=f'视频 {video.path} AI/TG 处理失败，将保留原文件名: {e}',
                target=task['source'],
                request_id=task['request_id'],
                data=result,
            )
        finally:
            for frame_path in frame_paths:
                try:
                    if os.path.exists(frame_path):
                        os.remove(frame_path)
                except OSError:
                    self.logger.debug(f'无法删除 AI 截图临时文件: {frame_path}')
            with self._lock:
                self._tasks.pop(task['uuid'], None)

    def _send_telegram_frames(self, task, frame_paths, result):
        video = task['video']
        args = task['args']
        if not telegram_enabled(args):
            return
        try:
            caption_template = telegram_config(args).get(
                'caption',
                '{TASKNAME} | 分段 {SEGMENT_ID} | {GAME}\n'
                '回复本相册中的任意图片：/update 游戏名',
            )
            caption = str(caption_template).format(
                TASKNAME=video.taskname or task['source'].split('/', 1)[-1],
                SEGMENT_ID=video.segment_id if video.segment_id is not None else '',
                GAME=result.get('main_game') or '未识别到游戏',
                BASENAME=os.path.basename(video.path),
            )
            album = send_frame_album(frame_paths, args, caption=caption)
            result['telegram'] = album
            self._register_telegram_album(task, album)
            self.logger.info(f'视频 {video.path} 的 {len(frame_paths)} 张截图已发送到 Telegram.')
        except Exception as error:
            # Notifications must never block recognition, upload or recording.
            self.logger.warning(f'视频 {video.path} 的 Telegram 截图发送失败: {error}')

    def _register_telegram_album(self, task, album):
        if not album or not album.get('message_ids'):
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
            'group_id': task['video'].group_id,
            'segment_id': task['video'].segment_id,
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
                        self.logger.warning('Telegram /update 的游戏名为空、过长或已被过滤，已忽略.')
                        continue
                    self._pipeSend(
                        event='telegram_update',
                        msg=f'Telegram 人工指定游戏名: {game}',
                        target=target['source'],
                        request_id=target['request_id'],
                        data={
                            'group_id': target['group_id'],
                            'segment_id': target['segment_id'],
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
        self.logger.info('AI rename stopped.')
