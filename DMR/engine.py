import queue
import logging
import threading

from .Cleaner import Cleaner
from .Downloader import Downloader
from .Render import Render
from .Uploader import Uploader
from .Task import ReplayTask
from .WebService import WebService
from .utils import *


_SENSITIVE_KEYS = {
    'api_key',
    'authorization',
    'access_token',
    'refresh_token',
    'bot_token',
    'token',
    'proxy',
}


def _redact_sensitive_data(value):
    if isinstance(value, dict):
        return {
            key: '***'
            if item and (
                str(key).lower() in _SENSITIVE_KEYS
                or (str(key).lower() == 'tg' and isinstance(item, str))
            )
            else _redact_sensitive_data(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_sensitive_data(item) for item in value]
    return value


class DMREngine():
    def __init__(self, notifier=None):
        self.logger = logging.getLogger(__name__)
        self.notifier = notifier
        self.task_dict = {}
        self.plugin_dict = {}
        self.recv_queue = None
        self.stoped = True
        
    def pipeSend(self, message:PipeMessage):
        target = message.target
        self.logger.debug(_redact_sensitive_data(message))
        if self.notifier and target.startswith('replay/') and message.source == 'downloader':
            try:
                taskname = target.split('/', 1)[1]
                if message.event == 'livestart':
                    self.notifier.live_started(taskname)
                elif message.event == 'liveend':
                    self.notifier.live_ended(taskname)
            except Exception as error:
                self.logger.debug(f'Telegram 直播事件通知失败: {error}')
        if target == 'engine':
            self.recv_queue.put(message)
        elif target.startswith('replay/'):
            taskname = target.split('/')[1]
            if taskname not in self.task_dict:
                self.logger.error(f'Task {taskname} not exists.')
                return
            self.task_dict[taskname]['send_queue'].put(message)
        elif target == 'render':
            self.plugin_dict['render']['send_queue'].put(message)
        elif target == 'uploader':
            self.plugin_dict['uploader']['send_queue'].put(message)
        elif target == 'cleaner':
            self.plugin_dict['cleaner']['send_queue'].put(message)
        elif target == 'ai_rename':
            self.plugin_dict['ai_rename']['send_queue'].put(message)
        elif target == 'downloader':
            self.plugin_dict['downloader']['send_queue'].put(message)
        else:
            # raise Exception(f'Unknown target {target}.')
            self.logger.error(f'Unknown target {target}.')

    def _pipeRecvMonitor(self):
        while not self.stoped:
            message:PipeMessage = self.recv_queue.get()
            try:
                if message.target == 'engine':
                    if message.event == 'info':
                        self.logger.info(message.msg)
                    elif message.event == 'addtask':
                        self.add_task(message.data['taskname'], message.data['config'])
                    elif message.event == 'deltask':
                        self.del_task(message.data)
                else:
                    self.pipeSend(message)
            except Exception as e:
                self.logger.error(f'Message:{_redact_sensitive_data(message)} raise an error.')
                self.logger.exception(e)
    
    def start(self):
        self.stoped = False
        self.recv_queue = queue.Queue()
        self._piperecvprocess = threading.Thread(target=self._pipeRecvMonitor, daemon=True)
        self._piperecvprocess.start()
        self.logger.debug('DMR engine started.')

        for name, plugin in self.plugin_dict.values():
            if plugin['status'] == 0:
                plugin['class'].start()
                self.plugin_dict['name']['status'] = 1
                self.logger.debug(f'Plugin {name} started.')
        
        for name, task in self.task_dict.values():
            if task['status'] == 0:
                task['class'].start()
                self.task_dict['name']['status'] = 1
                self.pipeSend(PipeMessage('engine', f'replay/{name}', 'ready'))
                self.logger.debug(f'Task {name} started.')

    def add_plugin(self, name, config):
        send_queue = queue.Queue()
        if name == 'render':
            plugin = Render((self.recv_queue, send_queue), **config)
        elif name == 'uploader':
            plugin = Uploader((self.recv_queue, send_queue), **config)
        elif name == 'cleaner':
            plugin = Cleaner((self.recv_queue, send_queue), **config)
        elif name == 'ai_rename':
            from .AIRename import AIRename
            plugin = AIRename((self.recv_queue, send_queue), **config)
        elif name == 'downloader':
            plugin = Downloader((self.recv_queue, send_queue), **config)
        elif name == 'webservice':
            plugin = WebService((self.recv_queue, send_queue), engine=self, **config)
        else:
            self.logger.error(f'Unknown plugin {name}.')
            # raise Exception(f'Unknown plugin {name}.')
        if self.stoped == False:
            plugin.start()
            self.logger.debug(f'Plugin {name} started.')
        else:
            self.logger.debug(f'Plugin {name} created.')
        self.plugin_dict[name] = {
            'class': plugin,
            'config': config,
            'send_queue': send_queue,
            'status': 0 if self.stoped else 1,
        }

    def add_task(self, taskname, config):
        send_queue = queue.Queue()
        task = ReplayTask(taskname, config, (self.recv_queue, send_queue))
        self.task_dict[taskname] = {
            'class': task,
            'config': config,
            'send_queue': send_queue,
            'status': 0 if self.stoped else 1,
        }
        if self.stoped == False:
            task.start()
            self.pipeSend(PipeMessage('engine', f'replay/{taskname}', 'ready'))
            self.logger.debug(f'Task {taskname} started.')
        else:
            self.logger.debug(f'Task {taskname} created.')

    def del_task(self, taskname):
        if taskname in self.task_dict:
            self.pipeSend(PipeMessage('engine', f'replay/{taskname}', 'exit'))
            # self.task_dict[taskname]['class'].stop()
            del self.task_dict[taskname]
            self.logger.debug(f'Task {taskname} deleted.')
        else:
            self.logger.debug(f'Task {taskname} not exists.')

    def stop(self):
        self.stoped = True
        for taskname in list(self.task_dict.keys()):
            self.del_task(taskname)
        for name in self.plugin_dict.keys():
            try:
                self.plugin_dict[name]['class'].stop()
            except Exception as e:
                self.logger.exception(e)
        self.task_dict.clear()
        self.plugin_dict.clear()
        self.recv_queue.put(PipeMessage('engine', 'engine', 'exit'))
        self.logger.info('DMR engine stoped.')
