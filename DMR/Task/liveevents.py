import logging
import os
from .baseevents import BaseEvents
from ..AIRename.naming import rename_video
from ..AIRename.parser import fixed_game_result, normalize_game
from ..utils import *

class LiveEvents(BaseEvents):
    def __init__(self, name, config):
        super().__init__(name, config)
        self.state_dict = {}
        self.ai_rename_dict = {}
        self.ended_dict = {}
        self.upload_request_dict = {}
        self.bv_title_dict = {}
        self.bv_manual_game_dict = {}
        self.bv_pending_manual_game_dict = {}
        self.telegram_screenshot_dict = {}
        self.logger = logging.getLogger(__name__)

    @property
    def event_dict(self):
        return {
            'ready': self.onReady,
            'exit': self.onExit,
            'downloader/livestart': self.defaultEvent, 
            'downloader/livesegment': self.onLiveSegment,
            'downloader/liveend': self.onLiveEnd,
            'downloader/livestop': self.onLiveEnd,
            'render/end': self.onRenderEnd,
            'render/error': self.defaultEvent,
            'ai_rename/end': self.onAIRenameEnd,
            'ai_rename/error': self.onAIRenameError,
            'ai_rename/screenshots_end': self.onTelegramScreenshotsEnd,
            'ai_rename/screenshots_error': self.onTelegramScreenshotsEnd,
            'ai_rename/telegram_update': self.onTelegramUpdate,
            'uploader/end': self.onUploadEnd,
            'uploader/error': self.defaultEvent,
            'uploader/title/end': self.onBVTitleEnd,
            'uploader/title/error': self.onBVTitleError,
            'cleaner/end': self.defaultEvent,
            'cleaner/error': self.defaultEvent,
            'default': self.defaultEvent,
        }
    
    def defaultEvent(self, message:PipeMessage):
        self.logger.info(f'{self.name}: {message.msg}')

    def onReady(self, *args, **kwargs):
        return PipeMessage(
            source=self.name,
            target='downloader',
            event='newtask',
            data={
                'dltype': self.config['download_args']['dltype'],
                'taskname': self.name,
                'config': self.config['download_args'],
            }
        )
    
    def onLiveSegment(self, message:PipeMessage):
        self.logger.info(f'{self.name}: {message.msg}')
        video:VideoInfo = message.data
        video_state = {
            # 'video_id': uuid(8),
            'src_video': {'status': None, 'file': None, 'wait': []},
            'src_video_pre': {'status': None, 'file': None, 'wait': []},
            'dm_video': {'status': None, 'file': None, 'wait': []},
        }
        if self.state_dict.get(video.group_id):
            self.state_dict[video.group_id].append(video_state)
        else:
            self.state_dict[video.group_id] = [video_state]

        ret_msgs = []
        ai_state = {
            'status': 'disabled',
            'request_id': None,
            'games': [],
            'main_game': '',
            'renamed_types': set(),
        }
        if self.config['common_event_args'].get('ai_rename'):
            ai_args = self.config.get('ai_rename_args', {})
            fixed_result = fixed_game_result(ai_args)
            if fixed_result is not None:
                ai_state.update({'status': 'ready', **fixed_result})
                if ai_state['main_game']:
                    self.logger.info(f'{self.name}: 使用固定游戏名: {ai_state["main_game"]}.')
                else:
                    self.logger.warning(f'{self.name}: fixed_game 已开启，但 game 为空或被过滤.')
            else:
                # Automatic game recognition has been removed. Screenshots are
                # collected once at live end for human review in Telegram.
                ai_state['status'] = 'ready'
        self.ai_rename_dict.setdefault(video.group_id, []).append(ai_state)

        if self.config['common_event_args'].get('auto_transcode'):
            transcode_args = self.config['render_args']['transcode']
            if transcode_args.get('output_name'):
                filename = replace_keywords(transcode_args['output_name'], video, replace_invalid=True) + \
                        f".{transcode_args.get('format','mp4')}"
            else:
                filename = os.path.splitext(os.path.basename(video.path))[0] + \
                        f"（转码后）.{transcode_args.get('format','mp4')}"
            if transcode_args.get('output_dir'):
                output_dir = transcode_args.get('output_dir')
            else:
                output_dir = os.path.dirname(video.path) + '（转码后）'
            output = os.path.join(output_dir, filename)
            transcode_msg = PipeMessage(
                source=self.name,
                target='render',
                event='newtask',
                request_id=uuid(),
                data={
                    'taskname': self.name,
                    'mode': 'transcode',
                    'video': video,
                    'output': output,
                    'args': transcode_args,
                }
            )
            self.state_dict[video.group_id][-1]['src_video_pre'].update({'status': 'ready', 'file': video})
            self.state_dict[video.group_id][-1]['src_video']['status'] = 'rendering'
            self.state_dict[video.group_id][-1]['src_video']['wait'].append(transcode_msg.request_id)
            ret_msgs.append(transcode_msg)
        else:
            self.state_dict[video.group_id][-1]['src_video'].update({'status': 'ready', 'file': video})
        
        if self.config['common_event_args'].get('auto_render'):
            render_args = self.config['render_args']['dmrender']
            if render_args.get('output_name'):
                filename = replace_keywords(render_args['output_name'], video, replace_invalid=True) + \
                        f".{render_args.get('format','mp4')}"
            else:
                filename = os.path.splitext(os.path.basename(video.path))[0] + \
                        f"（弹幕版）.{render_args.get('format','mp4')}"
            if render_args.get('output_dir'):
                output_dir = render_args.get('output_dir')
            else:
                output_dir = os.path.dirname(video.path) + '（弹幕版）'
            output = os.path.join(output_dir, filename)
            render_msg = PipeMessage(
                source=self.name,
                target='render',
                event='newtask',
                request_id=uuid(),
                data={
                    'taskname': self.name,
                    'mode': 'dmrender',
                    'video': video,
                    'output': output,
                    'args': render_args,
                }
            )
            self.state_dict[video.group_id][-1]['dm_video']['status'] = 'rendering'
            self.state_dict[video.group_id][-1]['dm_video']['wait'].append(render_msg.request_id)
            ret_msgs.append(render_msg)

        if ai_state['status'] == 'ready':
            self._apply_ai_rename(video.group_id, len(self.state_dict[video.group_id])-1)

        if self.config['common_event_args'].get('auto_upload'):
            ret_msgs += self._check_for_upload(video.group_id, len(self.state_dict[video.group_id])-1)
                
        return ret_msgs
    
    def onLiveEnd(self, message:PipeMessage):
        self.logger.info(f'{self.name}: {message.msg}.')
        group_id = message.data
        if group_id is None:
            return
        
        if group_id in self.state_dict:
            self.ended_dict[group_id] = time.time()
        else:
            self.logger.debug(f'No such group:{group_id}.')
        
        ret_msgs = []
        ret_msgs += self._queue_telegram_screenshots(group_id)
        if self.config['common_event_args'].get('auto_upload'):
            upload_msgs = self._check_for_upload(group_id)
            ret_msgs += upload_msgs

        ret_msgs += self._check_for_bv_title(group_id)

        self._free_state_memory()
        
        return ret_msgs

    def _queue_telegram_screenshots(self, group_id):
        if not self.config['common_event_args'].get('ai_rename'):
            return []
        args = self.config.get('ai_rename_args', {})
        tg_config = args.get('tg')
        tg_enabled = bool(tg_config) if isinstance(tg_config, str) \
            else bool((tg_config or {}).get('enabled'))
        if not tg_enabled or group_id not in self.state_dict:
            return []
        if group_id in self.telegram_screenshot_dict:
            return []

        videos = []
        for video_state in self.state_dict[group_id]:
            # Use the original segment so screenshots do not wait for rendering
            # or upload and remain available until the Telegram task finishes.
            video = None
            for vtype in ('src_video_pre', 'src_video', 'dm_video'):
                candidate = video_state.get(vtype, {}).get('file')
                if candidate and getattr(candidate, 'path', None):
                    video = candidate
                    break
            if video is not None:
                videos.append(video)

        if not videos:
            self.logger.warning(f'{self.name}: 直播组 {group_id} 没有可截图的视频.')
            return []

        request_id = uuid()
        self.telegram_screenshot_dict[group_id] = {
            'request_id': request_id,
            'pending': True,
        }
        return [PipeMessage(
            source=self.name,
            target='ai_rename',
            event='newtask',
            request_id=request_id,
            data={
                'taskname': self.name,
                'group_id': group_id,
                'videos': videos,
                'args': dict(args),
            },
        )]
    
    def _check_for_upload(self, group_id:str, _idx:int=None):
        ret_msgs = []
        if not self.state_dict.get(group_id):
            return ret_msgs
        
        upload_args = self.config['upload_args']
        for idx, video_state in enumerate(self.state_dict[group_id]):
            if _idx is not None and idx != _idx:
                continue
            for vtype, info in video_state.items():
                if info['status'] != 'ready':
                    continue
                if not self._ai_rename_allows_upload(group_id, idx, vtype):
                    continue
                for upload_file_types, upload_arg in upload_args.items():
                    # 判断当前视频是否需要上传
                    if vtype in upload_file_types.split('+'):
                        for upid, arg in enumerate(upload_arg):
                            # 实时上传
                            if not arg.get('realtime'):
                                continue
                            if info['file'].duration < arg.get('min_length', 0):
                                self.logger.info(f'视频{info["file"].path}时长为{info["file"].duration}s，设置{arg.get("min_length", 0)}s，跳过上传.')
                                continue
                            upload_group_id = info['file'].upload_group_id if hasattr(info['file'], 'upload_group_id') else group_id
                            upload_msg = PipeMessage(
                                source=self.name,
                                target='uploader',
                                event='newtask',
                                request_id=uuid(),
                                data={
                                    'taskname': self.name,
                                    'files': [info['file']],
                                    'engine': arg['engine'],
                                    'stateless': False,
                                    'upload_group': upload_group_id+'_'+upload_file_types+'_'+str(upid),
                                    'args': arg,
                                }
                            )
                            self.state_dict[group_id][idx][vtype]['status'] = 'uploading'
                            self.state_dict[group_id][idx][vtype]['wait'].append(upload_msg.request_id)
                            self._remember_upload_request(upload_msg, group_id)
                            ret_msgs.append(upload_msg)
        
        # 如果当前视频组已经被标记结束，检查是否有视频组完全准备好上传（用于非实时上传）
        if group_id in self.ended_dict:
            # 遍历所有视频类型
            video_types = list(self.state_dict[group_id][-1].keys())
            for vtype in video_types:
                # 检查是否全部准备上传
                videos = []
                for idx, video_state in enumerate(self.state_dict[group_id]):
                    if video_state[vtype]['status'] == 'ready' and \
                            self._ai_rename_allows_upload(group_id, idx, vtype):
                        videos.append(video_state[vtype]['file'])
                    else:
                        videos = []
                        break
                if not videos:
                    continue

                # 判断当前视频是否需要上传
                for upload_file_types, upload_arg in upload_args.items():
                    if vtype in upload_file_types.split('+'):
                        for upid, arg in enumerate(upload_arg):
                            # 此处只做非实时上传
                            if arg.get('realtime'):
                                continue
                            up_videos = [video for video in videos if video.duration >= arg.get('min_length', 0)]
                            upload_group_id = up_videos[0].upload_group_id if hasattr(up_videos[0], 'upload_group_id') else group_id
                            upload_msg = PipeMessage(
                                source=self.name,
                                target='uploader',
                                event='newtask',
                                request_id=uuid(),
                                data={
                                    'taskname': self.name,
                                    'files': up_videos,
                                    'engine': arg['engine'],
                                    'stateless': True,
                                    'upload_group': upload_group_id+'_'+upload_file_types+'_'+str(upid),
                                    'args': arg,
                                }
                            )
                            # 标记状态信息
                            for idx, _ in enumerate(self.state_dict[group_id]):
                                self.state_dict[group_id][idx][vtype]['status'] = 'uploading'
                                self.state_dict[group_id][idx][vtype]['wait'].append(upload_msg.request_id)
                            self._remember_upload_request(upload_msg, group_id)
                            ret_msgs.append(upload_msg)

        return ret_msgs
    
    def onRenderEnd(self, message:PipeMessage):
        self.logger.info(f'{self.name}: {message.msg}.')
        request_id = message.request_id
        video:VideoInfo = message.data.get('output')
        video_states = self.state_dict[video.group_id]
        # 将状态信息中request_id对应的等待移除
        for idx, video_state in enumerate(video_states):
            for vtype, info in video_state.items():
                if request_id in info['wait']:
                    self.state_dict[video.group_id][idx][vtype]['wait'].remove(request_id)
                    if len(self.state_dict[video.group_id][idx][vtype]['wait']) == 0:
                        self.state_dict[video.group_id][idx][vtype]['status'] = 'ready'
                        self.state_dict[video.group_id][idx][vtype]['file'] = video

        self._apply_ai_rename(video.group_id)
        
        ret_msgs = []
        if self.config['common_event_args'].get('auto_upload'):
            upload_msgs = self._check_for_upload(video.group_id)
            ret_msgs += upload_msgs

        return ret_msgs

    def _remember_upload_request(self, upload_msg, group_id):
        data = upload_msg.data
        args = data.get('args') or {}
        files = data.get('files') or []
        title = args.get('title', '')
        if title and files:
            try:
                title = replace_keywords(title, files[0])
            except Exception as e:
                self.logger.warning(f'解析上传标题失败: {e}')
                title = ''
        self.upload_request_dict[upload_msg.request_id] = {
            'group_id': group_id,
            'upload_group': data.get('upload_group'),
            'engine': data.get('engine'),
            'title': title,
            'args': {
                'account': args.get('account'),
                'cookies': args.get('cookies'),
                'limit': args.get('limit', 3),
            },
        }

    def _group_work_complete(self, group_id):
        if group_id not in self.ended_dict:
            return False
        if any(state['status'] == 'recognizing' for state in self.ai_rename_dict.get(group_id, [])):
            return False
        for video_state in self.state_dict.get(group_id, []):
            for info in video_state.values():
                if info['wait'] or info['status'] in ('rendering', 'uploading'):
                    return False
        return True

    def _check_for_bv_title(self, group_id):
        config = self.config.get('ai_rename_args', {})
        if not config.get('update_bv_title') or not self._group_work_complete(group_id):
            return []

        from ..AIRename.bvtitle import build_bv_title

        state = self.bv_title_dict.get(group_id)
        if not state:
            return []

        queued = state.setdefault('queued', {})
        if isinstance(queued, set):
            # Compatibility with state created before manual overrides were added.
            queued = state['queued'] = {}
        fixed_queued = state.setdefault('fixed_queued', set())
        fixed_applied = state.setdefault('fixed_applied', set())
        fixed_failed = state.setdefault('fixed_failed', set())
        for key in ('fixed_queued', 'fixed_applied', 'fixed_failed'):
            if not isinstance(state[key], set):
                state[key] = set(state[key] or [])
        fixed_queued = state['fixed_queued']
        fixed_applied = state['fixed_applied']
        fixed_failed = state['fixed_failed']

        uploads = [
            upload for upload in state['uploads'].values()
            if upload.get('bvid') and upload.get('engine') in ('biliuprs', 'biliwebapi')
        ]
        fixed_result = fixed_game_result(config)
        fixed_required = bool(fixed_result and fixed_result.get('main_game'))
        ret_msgs = []

        # A fixed game is a real first title phase. Manual replies are held until
        # every eligible BV has completed this phase, so a quick Telegram reply
        # cannot cancel the initial fixed title request.
        if fixed_required:
            for upload in uploads:
                bvid = upload['bvid']
                if bvid in fixed_applied or bvid in fixed_failed or bvid in fixed_queued:
                    continue
                title, games = build_bv_title(
                    upload.get('title'),
                    [fixed_result],
                    config,
                )
                if not title:
                    fixed_applied.add(bvid)
                    continue
                queued[bvid] = title
                fixed_queued.add(bvid)
                self.logger.info(f'视频组 {group_id} 固定游戏结果: {games}; 准备修改 {bvid} 标题.')
                ret_msgs.append(PipeMessage(
                    source=self.name,
                    target='uploader',
                    event='edit_bv_title',
                    request_id=uuid(),
                    data={
                        'bvid': bvid,
                        'title': title,
                        'group_id': group_id,
                        'phase': 'fixed',
                        'args': upload.get('args') or {},
                        'delay': config.get('bv_title_delay', 10),
                        'retries': config.get('bv_title_retries', 2),
                        'retry_interval': config.get('bv_title_retry_interval', 30),
                    },
                ))

            if any(
                upload['bvid'] not in fixed_applied and upload['bvid'] not in fixed_failed
                for upload in uploads
            ):
                return ret_msgs

        pending_game = self.bv_pending_manual_game_dict.get(group_id)
        if pending_game:
            self.bv_manual_game_dict[group_id] = pending_game

        manual_game = self.bv_manual_game_dict.get(group_id)
        title_states = self.ai_rename_dict.get(group_id, [])
        if manual_game:
            # A Telegram reply is authoritative for the whole BV, rather than
            # one more vote in the per-segment recognition statistics.
            title_states = [{'games': [manual_game], 'main_game': manual_game}]

        for upload in uploads:
            bvid = upload['bvid']
            title, games = build_bv_title(
                upload.get('title'),
                title_states,
                config,
            )
            if not title:
                self.logger.info(f'视频组 {group_id} 没有可用的游戏识别结果，不修改 {bvid} 标题.')
                continue
            if queued.get(bvid) == title:
                continue
            queued[bvid] = title
            source = 'Telegram 人工结果' if manual_game else '游戏统计结果'
            self.logger.info(f'视频组 {group_id} {source}: {games}; 准备修改 {bvid} 标题.')
            ret_msgs.append(PipeMessage(
                source=self.name,
                target='uploader',
                event='edit_bv_title',
                request_id=uuid(),
                data={
                    'bvid': bvid,
                    'title': title,
                    'group_id': group_id,
                    'phase': 'manual' if manual_game else 'normal',
                    'args': upload.get('args') or {},
                    'delay': config.get('bv_title_delay', 10),
                    'retries': config.get('bv_title_retries', 2),
                    'retry_interval': config.get('bv_title_retry_interval', 30),
                },
            ))
        return ret_msgs

    def _ai_target_types(self):
        target_types = self.config.get('ai_rename_args', {}).get('target_types', ['dm_video'])
        if not target_types:
            return set()
        if isinstance(target_types, str):
            target_types = target_types.replace(',', '+').split('+')
        return {str(vtype).strip() for vtype in target_types if str(vtype).strip()}

    def _find_ai_state(self, request_id):
        for group_id, states in self.ai_rename_dict.items():
            for idx, state in enumerate(states):
                if state['request_id'] == request_id:
                    return group_id, idx, state
        return None, None, None

    def _rename_dependencies_ready(self, video_state, vtype):
        if vtype not in ('src_video', 'src_video_pre'):
            return True
        return all(info['status'] != 'rendering' for info in video_state.values())

    def _apply_ai_rename(self, group_id, _idx=None):
        if not self.config['common_event_args'].get('ai_rename'):
            return
        if group_id not in self.state_dict or group_id not in self.ai_rename_dict:
            return

        target_types = self._ai_target_types()
        config = self.config.get('ai_rename_args', {})
        for idx, video_state in enumerate(self.state_dict[group_id]):
            if _idx is not None and idx != _idx:
                continue
            ai_state = self.ai_rename_dict[group_id][idx]
            if ai_state['status'] != 'ready' or not ai_state['main_game']:
                continue
            for vtype in target_types:
                info = video_state.get(vtype)
                if not info or info['status'] != 'ready' or not info['file']:
                    continue
                if vtype in ai_state['renamed_types']:
                    continue
                if not self._rename_dependencies_ready(video_state, vtype):
                    continue
                if not config.get('rename_files', False):
                    # Keep local filenames unchanged; only the final BV title is updated.
                    self.logger.info(
                        f'视频 {info["file"].path} 保持原文件名，AI 结果仅用于最终 B 站标题.'
                    )
                    ai_state['renamed_types'].add(vtype)
                    continue
                old_path = info['file'].path
                try:
                    new_path = rename_video(info['file'], ai_state['main_game'], config)
                    if new_path != old_path:
                        self.logger.info(f'视频已根据游戏名重命名: {old_path} -> {new_path}')
                except Exception as e:
                    self.logger.warning(f'视频 {old_path} 游戏名重命名失败，将保留原文件名: {e}')
                finally:
                    # 重命名失败也必须放行上传，不能让后处理链永久等待。
                    ai_state['renamed_types'].add(vtype)

    def _ai_rename_allows_upload(self, group_id, idx, vtype):
        if not self.config['common_event_args'].get('ai_rename'):
            return True
        try:
            ai_state = self.ai_rename_dict[group_id][idx]
        except (KeyError, IndexError):
            return False
        # 保留该状态检查，兼容旧状态或尚未完成的 Telegram 通知任务。
        if ai_state['status'] == 'recognizing':
            return False
        if vtype not in self._ai_target_types():
            return True
        if ai_state['status'] in ('disabled', 'failed'):
            return True
        if ai_state['status'] != 'ready':
            return False
        if not ai_state['main_game']:
            return True
        return vtype in ai_state['renamed_types']

    def onAIRenameEnd(self, message:PipeMessage):
        self.logger.info(f'{self.name}: {message.msg}.')
        group_id, idx, ai_state = self._find_ai_state(message.request_id)
        if ai_state is None:
            self.logger.debug(f'No AI rename state for request:{message.request_id}.')
            return
        data = message.data or {}
        ai_state.update({
            'status': 'ready',
            'games': data.get('games', []),
            'main_game': data.get('main_game', ''),
        })
        self._apply_ai_rename(group_id, idx)

        ret_msgs = []
        if self.config['common_event_args'].get('auto_upload'):
            ret_msgs += self._check_for_upload(group_id, idx)
        self._free_state_memory()
        return ret_msgs

    def onAIRenameError(self, message:PipeMessage):
        self.logger.warning(f'{self.name}: {message.msg}.')
        group_id, idx, ai_state = self._find_ai_state(message.request_id)
        if ai_state is None:
            return
        ai_state['status'] = 'failed'

        ret_msgs = []
        if self.config['common_event_args'].get('auto_upload'):
            ret_msgs += self._check_for_upload(group_id, idx)
        self._free_state_memory()
        return ret_msgs

    def onTelegramUpdate(self, message:PipeMessage):
        data = message.data or {}
        group_id, _idx, ai_state = self._find_ai_state(message.request_id)
        if group_id is None:
            group_id = data.get('group_id')
        if group_id not in self.state_dict:
            self.logger.info('Telegram 回复对应的视频状态已过期，已忽略.')
            return

        game = normalize_game(data.get('game', ''), self.config.get('ai_rename_args', {}))
        if not game:
            self.logger.warning('Telegram 回复的游戏名为空或被过滤，已忽略.')
            return

        self.bv_pending_manual_game_dict[group_id] = game
        if ai_state is not None:
            ai_state['manual_game'] = game
        fixed_result = fixed_game_result(self.config.get('ai_rename_args', {}))
        title_state = self.bv_title_dict.get(group_id) or {}
        fixed_done = not fixed_result or all(
            upload.get('bvid') in set(title_state.get('fixed_applied') or []) | set(title_state.get('fixed_failed') or [])
            for upload in title_state.get('uploads', {}).values()
            if upload.get('bvid') and upload.get('engine') in ('biliuprs', 'biliwebapi')
        ) and bool(title_state.get('uploads'))
        if not fixed_done and fixed_result and self.config.get('ai_rename_args', {}).get('update_bv_title'):
            self.logger.info(
                f'{self.name}: 已记录 Telegram 游戏名【{game}】，等待固定标题阶段完成后覆盖视频组 {group_id}.'
            )
            return []

        self.bv_manual_game_dict[group_id] = game
        self.logger.info(f'{self.name}: Telegram 人工覆盖视频组 {group_id} 的 BV 游戏前缀为【{game}】.')

        # Before upload completion this stores the override for the normal final edit;
        # after completion it queues another edit only when the target title changed.
        return self._check_for_bv_title(group_id)

    def onTelegramScreenshotsEnd(self, message:PipeMessage):
        data = message.data or {}
        group_id = data.get('group_id')
        if group_id is None:
            for candidate_group, state in self.telegram_screenshot_dict.items():
                if state.get('request_id') == message.request_id:
                    group_id = candidate_group
                    break
        if group_id is None:
            return
        screenshot_state = self.telegram_screenshot_dict.get(group_id)
        if screenshot_state and screenshot_state.get('request_id') != message.request_id:
            return
        if message.event == 'screenshots_error' or data.get('error'):
            self.logger.warning(f'{self.name}: 直播组 {group_id} Telegram 截图任务失败: {message.msg}')
        else:
            self.logger.info(f'{self.name}: 直播组 {group_id} Telegram 截图任务已结束.')
        self.telegram_screenshot_dict.pop(group_id, None)

        ret_msgs = []
        if self.config['common_event_args'].get('auto_clean'):
            ret_msgs += self._check_for_clean(group_id)
        self._free_state_memory()
        return ret_msgs

    def onBVTitleEnd(self, message:PipeMessage):
        data = message.data or {}
        group_id = data.get('group_id')
        bvid = data.get('bvid')
        phase = data.get('phase')
        if not group_id or not bvid:
            self.defaultEvent(message)
            return
        state = self.bv_title_dict.get(group_id)
        if state and phase == 'fixed':
            state.setdefault('fixed_queued', set()).discard(bvid)
            state.setdefault('fixed_applied', set()).add(bvid)
        return self._check_for_bv_title(group_id)

    def onBVTitleError(self, message:PipeMessage):
        data = message.data or {}
        group_id = data.get('group_id')
        bvid = data.get('bvid')
        phase = data.get('phase')
        if not group_id or not bvid:
            self.defaultEvent(message)
            return
        state = self.bv_title_dict.get(group_id)
        if state and phase == 'fixed':
            state.setdefault('fixed_queued', set()).discard(bvid)
            # Let a manual answer proceed even if the initial fixed update failed.
            state.setdefault('fixed_failed', set()).add(bvid)
        self.logger.warning(f'{self.name}: 视频组 {group_id} 的 BV {bvid} 标题修改失败，阶段: {phase}.')
        return self._check_for_bv_title(group_id)
    
    def _check_for_clean(self, group_id=None):
        ret_msgs = []
        clean_args = self.config['clean_args']
        for current_group_id, video_states in self.state_dict.items():
            if group_id is not None and current_group_id != group_id:
                continue
            if self.telegram_screenshot_dict.get(current_group_id, {}).get('pending'):
                continue
            for idx, video_state in enumerate(video_states):
                for vtype, info in video_state.items():
                    if info['status'] != 'uploaded':
                        continue
                    for clean_file_types, clean_arg in clean_args.items():
                        # 判断当前视频是否需要清理
                        if vtype in clean_file_types.split('+') or clean_file_types == 'all':
                            for arg in clean_arg:
                                files = [info['file']]
                                # 判断是否需要清理源文件
                                if vtype == 'dm_video' and arg.get('w_srcfile', False) == True and video_state['src_video']['file'] is not None:
                                    files.append(video_state['src_video']['file'])
                                    self.state_dict[current_group_id][idx]['src_video']['status'] = 'cleaned'
                                # 判断是否需要清理源文件（转码前）
                                if vtype == 'src_video' and arg.get('w_srcpre', True) == True and video_state['src_video_pre']['file'] is not None:
                                    files.append(video_state['src_video_pre']['file'])
                                    self.state_dict[current_group_id][idx]['src_video_pre']['status'] = 'cleaned'
                                
                                clean_msg = PipeMessage(
                                    source=self.name,
                                    target='cleaner',
                                    event='newtask',
                                    request_id=uuid(),
                                    data={
                                        'taskname': self.name,
                                        'files': files,
                                        'method': arg['method'],
                                        'delay': arg['delay'],
                                        'args': arg,
                                    }
                                )
                                ret_msgs.append(clean_msg)
                    self.state_dict[current_group_id][idx][vtype]['status'] = 'cleaned'

        return ret_msgs
    
    def _free_state_memory(self):
        final_status = 'ready'
        if self.config['common_event_args'].get('auto_upload'):
            final_status = 'uploaded'
        if self.config['common_event_args'].get('auto_clean'):
            final_status = 'cleaned'
        
        for group_id in list(self.ended_dict.keys()):
            need_free = True
            for idx, video_state in enumerate(self.state_dict[group_id]):
                for vtype, info in video_state.items():
                    if info['status'] is not None and info['status'] != final_status:
                        need_free = False
                        break
                if not need_free: break
            if need_free and self.config['common_event_args'].get('ai_rename'):
                if any(state['status'] == 'recognizing' for state in self.ai_rename_dict.get(group_id, [])):
                    need_free = False
            if self.telegram_screenshot_dict.get(group_id, {}).get('pending'):
                need_free = False
            tg_config = self.config.get('ai_rename_args', {}).get('tg') or {}
            if isinstance(tg_config, str):
                tg_config = {'enabled': True}
            reply_window = max(0, float(tg_config.get('reply_window', 86400)))
            if need_free and self.config.get('ai_rename_args', {}).get('update_bv_title') and tg_config.get('enabled') and reply_window and \
                    time.time() - self.ended_dict[group_id] < reply_window:
                # Keep only lightweight state long enough to accept a late Telegram reply.
                need_free = False
            if need_free:
                self.logger.debug(f'视频组{group_id}处理完成，视频信息已被释放.')
                self.ended_dict.pop(group_id)
                self.state_dict.pop(group_id)
                self.ai_rename_dict.pop(group_id, None)
                self.bv_title_dict.pop(group_id, None)
                self.bv_manual_game_dict.pop(group_id, None)
                self.bv_pending_manual_game_dict.pop(group_id, None)
                self.telegram_screenshot_dict.pop(group_id, None)
                self.upload_request_dict = {
                    request_id: upload
                    for request_id, upload in self.upload_request_dict.items()
                    if upload['group_id'] != group_id
                }

        for group_id in list(self.ended_dict.keys()):
            tg_config = self.config.get('ai_rename_args', {}).get('tg') or {}
            if isinstance(tg_config, str):
                tg_config = {'enabled': True}
            reply_window = max(0, float(tg_config.get('reply_window', 86400)))
            state_timeout = max(72 * 3600, reply_window)
            if time.time() - self.ended_dict[group_id] > state_timeout:
                self.logger.debug(f'视频组{group_id}处理超时，视频信息将被释放.')
                self.ended_dict.pop(group_id)
                self.state_dict.pop(group_id)
                self.ai_rename_dict.pop(group_id, None)
                self.bv_title_dict.pop(group_id, None)
                self.bv_manual_game_dict.pop(group_id, None)
                self.bv_pending_manual_game_dict.pop(group_id, None)
                self.telegram_screenshot_dict.pop(group_id, None)
                self.upload_request_dict = {
                    request_id: upload
                    for request_id, upload in self.upload_request_dict.items()
                    if upload['group_id'] != group_id
                }

    def onUploadEnd(self, message:PipeMessage):
        self.logger.info(f'{self.name}: {message.msg}.')
        request_id = message.request_id
        upload = self.upload_request_dict.pop(request_id, None)
        data = message.data or {}
        if upload and data.get('bvid'):
            group_id = upload['group_id']
            upload.update({
                'bvid': data.get('bvid'),
                'engine': data.get('engine') or upload.get('engine'),
                'upload_group': data.get('upload_group') or upload.get('upload_group'),
            })
            title_state = self.bv_title_dict.setdefault(group_id, {
                'uploads': {},
                'queued': {},
                'fixed_queued': set(),
                'fixed_applied': set(),
                'fixed_failed': set(),
            })
            existing_upload = title_state['uploads'].get(upload['upload_group'])
            if existing_upload:
                # A realtime BV is created by the first segment. Keep that segment's
                # resolved base title even when later append requests complete.
                existing_upload.update({
                    'bvid': upload['bvid'],
                    'engine': upload['engine'],
                })
            else:
                title_state['uploads'][upload['upload_group']] = upload
        # 将状态信息中request_id对应的等待移除
        matched_groups = set()
        for group_id, video_states in self.state_dict.items():
            for idx, video_state in enumerate(video_states):
                for vtype, info in video_state.items():
                    if request_id in info['wait']:
                        matched_groups.add(group_id)
                        self.state_dict[group_id][idx][vtype]['wait'].remove(request_id)
                        if len(self.state_dict[group_id][idx][vtype]['wait']) == 0:
                            self.state_dict[group_id][idx][vtype]['status'] = 'uploaded'
        
        ret_msgs = []
        for group_id in matched_groups:
            ret_msgs += self._check_for_bv_title(group_id)
        if self.config['common_event_args'].get('auto_clean'):
            clean_msgs = self._check_for_clean()
            ret_msgs += clean_msgs

        self._free_state_memory()
        return ret_msgs

    def onExit(self, *args, **kwargs) -> None:
        self.logger.info(f'{self.name}: 任务结束.')
        self.state_dict.clear()
        self.ai_rename_dict.clear()
        self.ended_dict.clear()
        self.upload_request_dict.clear()
        self.bv_title_dict.clear()
        self.bv_manual_game_dict.clear()
        self.bv_pending_manual_game_dict.clear()
        self.telegram_screenshot_dict.clear()
        return PipeMessage(
            source=self.name,
            target='downloader',
            event='stoptask',
            data=self.name,
        )
