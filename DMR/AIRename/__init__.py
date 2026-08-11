import logging
import os
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Tuple

from DMR.utils import PipeMessage, VideoInfo, uuid

from .cache import AIRenameCache
from .client import OpenAICompatibleVisionClient
from .frames import extract_frames
from .parser import parse_game_result


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
            cache = AIRenameCache(args.get('cache_file', '.temp/ai_rename_cache.json'))
            cache_key = cache.make_key(video, args)
            cached = cache.get(cache_key) if args.get('cache', True) else None
            if cached is not None:
                result.update(cached)
            else:
                frame_paths = extract_frames(video, args)
                response_text = OpenAICompatibleVisionClient(args).recognize(frame_paths)
                result.update(parse_game_result(response_text, args))
                if args.get('cache', True):
                    cache.set(cache_key, {
                        'games': result['games'],
                        'main_game': result['main_game'],
                    })

            game = result['main_game'] or '未识别到游戏'
            self._pipeSend(
                event='end',
                msg=f'视频 {video.path} AI 识别完成: {game}',
                target=task['source'],
                request_id=task['request_id'],
                data=result,
            )
        except Exception as e:
            result['error'] = str(e)
            self.logger.warning(f'视频 {video.path} AI 识别失败，将保留原文件名: {e}')
            self._pipeSend(
                event='error',
                msg=f'视频 {video.path} AI 识别失败，将保留原文件名: {e}',
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

    def stop(self):
        self.stoped = True
        self._executors.shutdown(wait=False)
        self.logger.info('AI rename stopped.')
