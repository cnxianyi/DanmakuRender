import logging
import platform
import queue
import threading
import time
from urllib.parse import urlsplit

import requests


def _proxy_mapping(proxy):
    if not proxy:
        return None
    proxy = str(proxy).strip()
    if not proxy:
        return None
    if '://' not in proxy:
        proxy = f'http://{proxy}'
    parsed = urlsplit(proxy)
    if parsed.scheme.lower() not in {'http', 'https', 'socks5', 'socks5h'} or not parsed.netloc:
        raise ValueError(
            'tg.proxy 必须是 http://、https://、socks5:// 或 socks5h:// 代理地址'
        )
    return {'http': proxy, 'https': proxy}


class TelegramNotifier:
    """Send best-effort Telegram notifications for application and live events."""

    def __init__(self, config=None, logger=None):
        if isinstance(config, str):
            config = {'enabled': True, 'bot_token': config}
        self.config = config if isinstance(config, dict) else {}
        self.logger = logger or logging.getLogger('DMR.TelegramNotification')
        self.token = str(
            self.config.get('bot_token') or self.config.get('token') or ''
        ).strip()
        self.chat_id = str(self.config.get('chat_id') or '').strip()
        self.api_base = str(
            self.config.get('api_base') or 'https://api.telegram.org'
        ).rstrip('/')
        self.enabled = bool(self.config.get('enabled', False)) and bool(
            self.token and self.chat_id
        )
        self.timeout = max(
            1,
            float(self.config.get(
                'notification_timeout',
                self.config.get('timeout', 60),
            )),
        )
        self.cooldown = max(
            0,
            float(self.config.get(
                'notification_cooldown',
                self.config.get('cooldown', 300),
            )),
        )
        self.message_thread_id = self.config.get('message_thread_id')
        self.disable_notification = bool(self.config.get('disable_notification', False))
        self.notify_live_start = bool(self.config.get('notify_live_start', True))
        self.notify_live_end = bool(self.config.get('notify_live_end', True))
        self.notify_errors = bool(self.config.get('notify_errors', True))
        self.machine_name = str(
            self.config.get('machine_name') or platform.node() or 'unknown'
        )
        self._last_sent = {}
        self._live_tasks = set()
        self._lock = threading.Lock()
        self._handler = None
        self._queue = queue.Queue()
        self._worker = None
        self._closed = False

        try:
            self.proxies = _proxy_mapping(self.config.get('proxy'))
        except ValueError as error:
            self.proxies = None
            self.logger.warning(f'Telegram 通知配置无效，通知已禁用: {error}')
            self.enabled = False

        if self.enabled:
            self._worker = threading.Thread(
                target=self._send_worker,
                name='telegram-notifier',
                daemon=True,
            )
            self._worker.start()

    def install_logging_handler(self):
        """Forward ERROR/CRITICAL records from the DMR logger to Telegram."""
        if not self.enabled or not self.notify_errors:
            return
        if self._handler is not None:
            return
        handler = TelegramLoggingHandler(self)
        handler.setFormatter(logging.Formatter('%(name)s: %(message)s'))
        self.logger.addHandler(handler)
        self._handler = handler

    def notify(self, title, body, cooldown_key='general'):
        if not self.enabled:
            return False

        now = time.time()
        with self._lock:
            if self._closed:
                return False
            last_sent = self._last_sent.get(cooldown_key)
            if (
                last_sent is not None
                and self.cooldown
                and now - last_sent < self.cooldown
            ):
                return False
            self._last_sent[cooldown_key] = now

        self._queue.put({
            'title': str(title),
            'body': str(body),
        })
        return True

    def _send_worker(self):
        while True:
            payload = self._queue.get()
            try:
                if payload is None:
                    return
                self._send_payload(payload)
            finally:
                self._queue.task_done()

    def _send_payload(self, payload):
        text = payload['title']
        if payload['body']:
            text += f'\n{payload["body"]}'
        data = {
            'chat_id': self.chat_id,
            'text': text,
        }
        if self.message_thread_id not in (None, ''):
            data['message_thread_id'] = str(self.message_thread_id)
        if self.disable_notification:
            data['disable_notification'] = 'true'
        try:
            response = requests.post(
                f'{self.api_base}/bot{self.token}/sendMessage',
                data=data,
                timeout=self.timeout,
                proxies=self.proxies,
            )
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict) or not result.get('ok'):
                raise RuntimeError(
                    result.get('description') if isinstance(result, dict)
                    else 'Telegram 返回格式无效'
                )
        except (OSError, requests.RequestException, ValueError, RuntimeError) as error:
            self.logger.warning(f'Telegram 通知发送失败: {error}')
        except Exception as error:
            self.logger.warning(f'Telegram 通知发送失败: {error}')

    def live_started(self, task_name):
        task_name = str(task_name or '').strip() or 'unknown'
        with self._lock:
            if task_name in self._live_tasks:
                return False
            self._live_tasks.add(task_name)
        if not self.notify_live_start:
            return False
        return self.notify(
            '直播开始',
            task_name,
            cooldown_key=f'live-start:{task_name}',
        )

    def live_ended(self, task_name):
        task_name = str(task_name or '').strip() or 'unknown'
        with self._lock:
            if task_name not in self._live_tasks:
                return False
            self._live_tasks.remove(task_name)
        if not self.notify_live_end:
            return False
        return self.notify(
            '直播结束',
            task_name,
            cooldown_key=f'live-end:{task_name}',
        )

    def process_stopped(self, body='Python 进程已停止'):
        return self.notify(
            f'DanmakuRender stopped on {self.machine_name}',
            body,
            cooldown_key='process-stop',
        )

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
        if self._worker is not None:
            deadline = time.monotonic() + min(5, self.timeout)
            while self._queue.unfinished_tasks and time.monotonic() < deadline:
                time.sleep(0.01)
            self._queue.put(None)
            self._worker.join(max(0, deadline - time.monotonic()))
            self._worker = None
        if self._handler is not None:
            self.logger.removeHandler(self._handler)
            self._handler.close()
            self._handler = None


class TelegramLoggingHandler(logging.Handler):
    def __init__(self, notifier):
        super().__init__(level=logging.ERROR)
        self.notifier = notifier

    def emit(self, record):
        try:
            body = self.format(record)
            if len(body) > 800:
                body = body[:800] + '...'
            self.notifier.notify(
                f'DanmakuRender error on {self.notifier.machine_name}',
                body,
                cooldown_key='error',
            )
        except Exception:
            # Notification failures must never interfere with normal logging.
            pass
