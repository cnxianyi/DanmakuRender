import logging
import os
import shutil
import threading
import json
import time
import logging

from .engine import DMREngine
from .Config import Config
from .utils import filename_to_taskname


def _redact_sensitive_config(value):
    if isinstance(value, dict):
        return {
            key: '***' if item and (
                str(key).lower() in ('api_key', 'bot_token', 'token')
                or (str(key).lower() == 'tg' and isinstance(item, str))
            )
            else _redact_sensitive_config(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_sensitive_config(item) for item in value]
    return value


class DanmakuRender():
    def __init__(self, config:Config, **kwargs) -> None:
        self.logger = logging.getLogger('DMR')
        self.config = config
        self.kwargs = kwargs
        self.stoped = True
        self.engine_args = self.config.get_config('dmr_engine_args')
        self.engine = DMREngine()

    def start(self):
        self.stoped = False
        os.makedirs('.temp', exist_ok=True)
        
        global_config_log = _redact_sensitive_config(self.config.global_config)
        replay_config_log = _redact_sensitive_config(self.config.replay_config)
        self.logger.debug(f'Global Config:\n{json.dumps(global_config_log, indent=4, ensure_ascii=False)}')
        self.logger.debug(f'Replay Config:\n{json.dumps(replay_config_log, indent=4, ensure_ascii=False)}')
        self.engine.start()
        plugin_enabled = list(self.config.get_config('dmr_engine_args')['enabled_plugins'])
        for plugin_name in plugin_enabled:
            plugin_config = self.config.get_config(plugin_name+'_kernel_args')
            self.engine.add_plugin(plugin_name, plugin_config)

        for taskname in self.config.get_replaytasks():
            replay_config = self.config.get_replay_config(taskname)
            self._ensure_task_plugins(replay_config)
            self.engine.add_task(taskname, replay_config)

        threading.Thread(target=self._monintor, daemon=True).start()

    def _ensure_task_plugins(self, replay_config):
        ai_args = replay_config.get('ai_rename_args', {})
        tg_config = ai_args.get('tg')
        tg_enabled = bool(tg_config) if isinstance(tg_config, str) \
            else bool((tg_config or {}).get('enabled'))
        if replay_config['common_event_args'].get('ai_rename') and \
                (not ai_args.get('fixed_game') or tg_enabled) and \
                'ai_rename' not in self.engine.plugin_dict:
            self.engine.add_plugin(
                'ai_rename',
                self.config.get_config('ai_rename_kernel_args') or {},
            )

    def check_config_update(self):
        try:
            update_type, update_info = self.config.check_update()
            
            if update_type == 'global':
                self.logger.info('检测到全局配置更新，请重启程序以生效。')
            
            elif update_type == 'tasks':
                for config_path in update_info['new']:
                    taskname = filename_to_taskname(config_path)
                    self.logger.info(f'检测到新任务配置文件: {taskname}，正在添加任务...')
                    replay_config = self.config.get_replay_config(taskname)
                    self._ensure_task_plugins(replay_config)
                    self.engine.add_task(taskname, replay_config)
                
                for config_path in update_info['deleted']:
                    taskname = filename_to_taskname(config_path)
                    self.logger.info(f'检测到任务配置文件删除: {taskname}，正在停止任务...')
                    self.engine.del_task(taskname)

                for config_path in update_info['updated']:
                    taskname = filename_to_taskname(config_path)
                    self.logger.info(f'检测到任务配置文件更新: {taskname}，正在重启任务...')
                    
                    self.engine.del_task(taskname)
                    time.sleep(5)
                    new_taskname = filename_to_taskname(config_path)
                    replay_config = self.config.get_replay_config(new_taskname)
                    self._ensure_task_plugins(replay_config)
                    self.engine.add_task(new_taskname, replay_config)

        except Exception as e:
            self.logger.error(f'动态载入配置文件错误:')
            self.logger.exception(e)

    def _monintor(self):
        REFRESH_INTERVAL = 60
        time.sleep(REFRESH_INTERVAL)
        while not self.stoped:
            self.check_config_update()
            # clean temp file
            files = os.listdir('.temp')
            for file in files:
                try:
                    basename = os.path.splitext(os.path.basename(file))[0]
                    expired_time = basename.split('_')[-1]
                    if expired_time.isdigit():
                        expired_time = int(expired_time)
                    else:
                        expired_time = 0
                    # 只清理2024.01.01之后的过期文件，过早的文件认为不是程序创建的不清理
                    if expired_time > 1704038400 and expired_time < int(time.time()):
                        file = os.path.join('.temp', file)
                        if os.path.isfile(file):
                            os.remove(file)
                            self.logger.debug(f'已清理临时文件: {file}')
                        elif os.path.isdir(file):
                            shutil.rmtree(file)
                            self.logger.debug(f'已清理临时文件夹: {file}')
                except Exception as e:
                    self.logger.debug(f'清理临时文件{file}失败: {e}')
            
            time.sleep(REFRESH_INTERVAL)

    def stop(self):
        self.stoped = True
        self.engine.stop()
