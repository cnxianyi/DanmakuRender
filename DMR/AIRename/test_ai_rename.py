import os
import json
import tempfile
import unittest
from unittest.mock import patch

from DMR.AIRename.bvtitle import build_bv_title, rank_games
from DMR import _redact_sensitive_config
from DMR.engine import _redact_sensitive_data
from DMR.AIRename.naming import rename_video
from DMR.AIRename.parser import fixed_game_result
from DMR.AIRename.telegram import (
    get_updates,
    parse_update_reply,
    send_frame_album,
    telegram_config,
    telegram_proxies,
)
from DMR.AIRename.screenshots import extract_first_frame
from DMR.Task.liveevents import LiveEvents
from DMR.utils import PipeMessage, VideoInfo


class GameNameParserTests(unittest.TestCase):
    def test_fixed_game_returns_normalized_result(self):
        result = fixed_game_result({
            'fixed_game': True,
            'game': '这是一个超过十个字的游戏名称',
        })

        self.assertEqual(result, {
            'games': ['这是一个超过十个字的'],
            'main_game': '这是一个超过十个字的',
        })

    def test_fixed_game_disabled_returns_none(self):
        self.assertIsNone(fixed_game_result({'fixed_game': False, 'game': '三角洲行动'}))


class SensitiveLoggingTests(unittest.TestCase):
    def test_redacts_nested_telegram_credentials(self):
        proxy = 'http://user:' + 'test-password' + '@127.0.0.1:7890'
        result = _redact_sensitive_data({
            'args': {
                'tg': {'bot_token': 'telegram-secret'},
                'proxy': proxy,
            },
        })

        self.assertEqual(result['args']['tg']['bot_token'], '***')
        self.assertEqual(result['args']['proxy'], '***')

    def test_redacts_telegram_token_shorthand(self):
        value = {'ai_rename_args': {'tg': '123456:ABCDEF'}}
        self.assertEqual(
            _redact_sensitive_data(value)['ai_rename_args']['tg'],
            '***',
        )
        self.assertEqual(
            _redact_sensitive_config(value)['ai_rename_args']['tg'],
            '***',
        )


class TelegramTests(unittest.TestCase):
    def test_accepts_token_only_shorthand(self):
        self.assertEqual(
            telegram_config({'tg': '123456:ABCDEF'}),
            {'enabled': True, 'bot_token': '123456:ABCDEF'},
        )

    def test_builds_http_and_socks5_proxy_mappings(self):
        self.assertEqual(
            telegram_proxies({'proxy': '127.0.0.1:7890'}),
            {
                'http': 'http://127.0.0.1:7890',
                'https': 'http://127.0.0.1:7890',
            },
        )
        self.assertEqual(
            telegram_proxies({'proxy': 'socks5h://127.0.0.1:1080'}),
            {
                'http': 'socks5h://127.0.0.1:1080',
                'https': 'socks5h://127.0.0.1:1080',
            },
        )
        with self.assertRaises(ValueError):
            telegram_proxies({'proxy': 'ftp://127.0.0.1:21'})

    def test_parses_direct_reply_and_legacy_update_command(self):
        command = parse_update_reply({
            'update_id': 88,
            'message': {
                'message_id': 200,
                'chat': {'id': 123456},
                'text': '瓦',
                'reply_to_message': {'message_id': 101},
            },
        })

        self.assertEqual(command, {
            'update_id': 88,
            'chat_id': '123456',
            'reply_message_id': 101,
            'message_id': 200,
            'game': '瓦',
        })

        legacy = parse_update_reply({
            'message': {
                'message_id': 201,
                'chat': {'id': 123456},
                'text': '/update 瓦',
                'reply_to_message': {'message_id': 101},
            },
        })
        self.assertEqual(legacy['game'], '瓦')

        self.assertIsNone(parse_update_reply({
            'message': {
                'chat': {'id': 123456},
                'text': '瓦',
            },
        }))

    @patch('DMR.AIRename.telegram.requests.post')
    def test_sends_frame_album_and_returns_all_message_ids(self, post):
        post.return_value.ok = True
        post.return_value.json.return_value = {
            'ok': True,
            'result': [
                {'message_id': 301, 'chat': {'id': 123456}, 'media_group_id': 'album-1'},
                {'message_id': 302, 'chat': {'id': 123456}, 'media_group_id': 'album-1'},
            ],
        }

        with tempfile.TemporaryDirectory() as directory:
            frames = []
            for index in range(2):
                path = os.path.join(directory, f'{index}.jpg')
                with open(path, 'wb') as file:
                    file.write(b'jpeg')
                frames.append(path)

            sent = send_frame_album(frames, {
                'tg': {
                    'enabled': True,
                    'bot_token': 'telegram-token',
                    'chat_id': '123456',
                    'proxy': 'http://127.0.0.1:7890',
                    'retries': 0,
                },
            }, caption='reply with /update game')

        self.assertEqual(sent, {
            'chat_id': '123456',
            'message_ids': [301, 302],
            'media_group_id': 'album-1',
            'media_group_ids': ['album-1'],
        })
        self.assertTrue(post.call_args.args[0].endswith('/bottelegram-token/sendMediaGroup'))
        self.assertEqual(
            json.loads(post.call_args.kwargs['data']['media'])[0]['caption'],
            'reply with /update game',
        )
        self.assertEqual(
            post.call_args.kwargs['proxies'],
            {
                'http': 'http://127.0.0.1:7890',
                'https': 'http://127.0.0.1:7890',
            },
        )

    @patch('DMR.AIRename.telegram.requests.get')
    def test_get_updates_uses_configured_proxy(self, get):
        get.return_value.json.return_value = {'ok': True, 'result': []}
        get_updates({
            'tg': {
                'bot_token': 'telegram-token',
                'proxy': 'socks5://127.0.0.1:1080',
            },
        })

        self.assertEqual(
            get.call_args.kwargs['proxies'],
            {
                'http': 'socks5://127.0.0.1:1080',
                'https': 'socks5://127.0.0.1:1080',
            },
        )


class ScreenshotTests(unittest.TestCase):
    @patch('DMR.AIRename.screenshots.subprocess.run')
    @patch('DMR.AIRename.screenshots.ToolsList.get', return_value='ffmpeg')
    def test_extracts_the_first_frame(self, _ffmpeg, run):
        def create_frame(command, **kwargs):
            with open(command[-1], 'wb') as file:
                file.write(b'jpeg')
            run.return_value.returncode = 0
            run.return_value.stderr = b''
            return run.return_value

        run.side_effect = create_frame
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, 'video.mp4')
            with open(source, 'wb') as file:
                file.write(b'video')
            frame = extract_first_frame(VideoInfo(path=source, duration=10), {})
            try:
                command = run.call_args.args[0]
                self.assertNotIn('-ss', command)
                self.assertNotIn('-vf', command)
                self.assertEqual(command[command.index('-i') + 1], source)
                self.assertEqual(command[command.index('-frames:v') + 1], '1')
            finally:
                if os.path.exists(frame):
                    os.remove(frame)


class BVTitleTests(unittest.TestCase):
    def test_ranks_segment_game_names_by_count(self):
        states = [
            {'games': ['三角洲行动']},
            {'games': ['幻兽帕鲁']},
            {'games': ['三角洲行动']},
        ]

        self.assertEqual(rank_games(states), ['三角洲行动', '幻兽帕鲁'])
        title, games = build_bv_title(
            '[oyo/直播回放] 伦敦未必有我忧郁 2026年07月21日',
            states,
            {},
        )
        self.assertEqual(games, ['三角洲行动', '幻兽帕鲁'])
        self.assertEqual(
            title,
            '【三角洲行动|幻兽帕鲁】[oyo/直播回放] 伦敦未必有我忧郁 2026年07月21日',
        )

    def test_empty_results_do_not_change_title(self):
        title, games = build_bv_title('original', [{'games': ['', '', '']}], {})
        self.assertEqual((title, games), ('', []))

    def test_title_uses_at_most_two_games_and_truncates_long_names(self):
        states = [
            {'games': ['这是一个超过十个字的游戏名称']},
            {'games': ['三角洲行动']},
            {'games': ['幻兽帕鲁']},
        ]

        title, games = build_bv_title('original', states, {})

        self.assertEqual(len(games), 2)
        self.assertTrue(all(len(game) <= 10 for game in games))
        self.assertEqual(title, '【这是一个超过十个字的|三角洲行动】original')


class AIRenameNamingTests(unittest.TestCase):
    def test_adds_prefix_and_updates_video_path(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, 'oyo-2026年08月01日00点39分（弹幕版）.mp4')
            open(source, 'wb').close()
            video = VideoInfo(path=source)

            renamed = rename_video(video, '三角洲行动', {})

            self.assertEqual(
                os.path.basename(renamed),
                '【三角洲行动】oyo-2026年08月01日00点39分（弹幕版）.mp4',
            )
            self.assertEqual(video.path, renamed)
            self.assertTrue(os.path.exists(renamed))

    def test_does_not_add_a_second_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, '【三角洲行动】oyo.mp4')
            open(source, 'wb').close()
            video = VideoInfo(path=source)

            self.assertEqual(rename_video(video, '三角洲行动', {}), source)


class LiveEventsAIRenameTests(unittest.TestCase):
    def _config(self):
        return {
            'common_event_args': {
                'auto_render': False,
                'auto_transcode': False,
                'auto_upload': False,
                'auto_clean': False,
                'ai_rename': True,
            },
            'download_args': {'dltype': 'live'},
            'ai_rename_args': {'target_types': ['dm_video'], 'rename_files': True},
        }

    def test_segment_skips_automatic_recognition_and_allows_upload(self):
        events = LiveEvents('test', self._config())
        video = VideoInfo(
            path='input.flv',
            group_id='group',
            segment_id=1,
            duration=60,
        )

        messages = events.onLiveSegment(PipeMessage(
            source='downloader',
            target='replay/test',
            event='livesegment',
            data=video,
        ))

        self.assertFalse(any(message.target == 'ai_rename' for message in messages))
        self.assertEqual(events.ai_rename_dict['group'][0]['status'], 'ready')
        self.assertTrue(events._ai_rename_allows_upload('group', 0, 'src_video'))

    def test_fixed_game_skips_ai_request(self):
        config = self._config()
        config['ai_rename_args'].update({
            'fixed_game': True,
            'game': '三角洲行动',
        })
        events = LiveEvents('test', config)
        video = VideoInfo(
            path='input.flv',
            group_id='group',
            segment_id=1,
            duration=60,
        )

        messages = events.onLiveSegment(PipeMessage(
            source='downloader',
            target='replay/test',
            event='livesegment',
            data=video,
        ))

        self.assertFalse(any(message.target == 'ai_rename' for message in messages))
        self.assertEqual(events.ai_rename_dict['group'][0]['status'], 'ready')
        self.assertEqual(events.ai_rename_dict['group'][0]['games'], ['三角洲行动'])

    def test_live_end_queues_one_group_screenshot_task(self):
        config = self._config()
        config['ai_rename_args'].update({
            'fixed_game': True,
            'game': '三角洲行动',
            'tg': {'enabled': True, 'bot_token': 'token', 'chat_id': '123'},
        })
        events = LiveEvents('test', config)
        video = VideoInfo(
            path='input.flv',
            group_id='group',
            segment_id=1,
            duration=60,
        )

        messages = events.onLiveSegment(PipeMessage(
            source='downloader',
            target='replay/test',
            event='livesegment',
            data=video,
        ))

        self.assertFalse(any(message.target == 'ai_rename' for message in messages))
        end_messages = events.onLiveEnd(PipeMessage(
            source='downloader',
            target='replay/test',
            event='liveend',
            data='group',
        ))
        ai_messages = [message for message in end_messages if message.target == 'ai_rename']
        self.assertEqual(len(ai_messages), 1)
        self.assertEqual(len(ai_messages[0].data['videos']), 1)
        self.assertEqual(ai_messages[0].data['group_id'], 'group')
        self.assertEqual(
            events.ai_rename_dict['group'][0]['main_game'],
            '三角洲行动',
        )
        self.assertEqual(events.ai_rename_dict['group'][0]['status'], 'ready')

    def test_ai_callback_renames_ready_target(self):
        with tempfile.TemporaryDirectory() as directory:
            events = LiveEvents('test', self._config())
            source = os.path.join(directory, 'oyo（弹幕版）.mp4')
            open(source, 'wb').close()
            video = VideoInfo(
                path=source,
                group_id='group',
                segment_id=1,
                duration=60,
            )
            request_id = 'ai-request'
            events.state_dict['group'] = [{
                'src_video': {'status': None, 'file': None, 'wait': []},
                'src_video_pre': {'status': None, 'file': None, 'wait': []},
                'dm_video': {'status': 'ready', 'file': video, 'wait': []},
            }]
            events.ai_rename_dict['group'] = [{
                'status': 'recognizing',
                'request_id': request_id,
                'games': [],
                'main_game': '',
                'renamed_types': set(),
            }]

            events.onAIRenameEnd(PipeMessage(
                source='ai_rename',
                target='replay/test',
                event='end',
                request_id=request_id,
                data={
                    'games': ['三角洲行动', '三角洲行动', ''],
                    'main_game': '三角洲行动',
                },
            ))

            self.assertTrue(os.path.basename(video.path).startswith('【三角洲行动】'))
            self.assertTrue(events._ai_rename_allows_upload('group', 0, 'dm_video'))

    def test_ai_callback_keeps_local_filename_when_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._config()
            config['ai_rename_args']['rename_files'] = False
            events = LiveEvents('test', config)
            source = os.path.join(directory, 'oyo.mp4')
            open(source, 'wb').close()
            video = VideoInfo(
                path=source,
                group_id='group',
                segment_id=1,
                duration=60,
            )
            request_id = 'ai-request'
            events.state_dict['group'] = [{
                'src_video': {'status': None, 'file': None, 'wait': []},
                'src_video_pre': {'status': None, 'file': None, 'wait': []},
                'dm_video': {'status': 'ready', 'file': video, 'wait': []},
            }]
            events.ai_rename_dict['group'] = [{
                'status': 'recognizing',
                'request_id': request_id,
                'games': [],
                'main_game': '',
                'renamed_types': set(),
            }]

            events.onAIRenameEnd(PipeMessage(
                source='ai_rename',
                target='replay/test',
                event='end',
                request_id=request_id,
                data={
                    'games': ['三角洲行动', '三角洲行动', ''],
                    'main_game': '三角洲行动',
                },
            ))

            self.assertEqual(video.path, source)
            self.assertTrue(events._ai_rename_allows_upload('group', 0, 'dm_video'))

    def test_queues_bv_title_edit_only_after_group_is_complete(self):
        config = self._config()
        config['common_event_args']['auto_upload'] = True
        config['ai_rename_args']['update_bv_title'] = True
        config['upload_args'] = {}
        events = LiveEvents('test', config)
        events.ended_dict['group'] = 1
        events.state_dict['group'] = [{
            'src_video': {'status': None, 'file': None, 'wait': []},
            'src_video_pre': {'status': None, 'file': None, 'wait': []},
            'dm_video': {'status': 'uploaded', 'file': VideoInfo(path='video.mp4'), 'wait': []},
        }]
        events.ai_rename_dict['group'] = [{
            'status': 'ready',
            'request_id': 'ai-request',
            'games': ['三角洲行动', '三角洲行动', '幻兽帕鲁'],
            'main_game': '三角洲行动',
            'renamed_types': {'dm_video'},
        }]
        events.bv_title_dict['group'] = {
            'uploads': {
                'group_dm_video_0': {
                    'bvid': 'BV1234567890',
                    'engine': 'biliuprs',
                    'title': '[oyo/直播回放] 伦敦未必有我忧郁 2026年07月21日',
                    'args': {'account': 'bilibili'},
                },
            },
            'queued': set(),
        }

        messages = events._check_for_bv_title('group')

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].event, 'edit_bv_title')
        self.assertTrue(messages[0].data['title'].startswith('【三角洲行动|幻兽帕鲁】'))
        self.assertEqual(events._check_for_bv_title('group'), [])

        override = events.onTelegramUpdate(PipeMessage(
            source='ai_rename',
            target='replay/test',
            event='telegram_update',
            request_id='ai-request',
            data={'group_id': 'group', 'game': '瓦'},
        ))

        self.assertEqual(len(override), 1)
        self.assertTrue(override[0].data['title'].startswith('【瓦】'))
        self.assertNotIn('三角洲行动', override[0].data['title'])
        self.assertEqual(events.onTelegramUpdate(PipeMessage(
            source='ai_rename',
            target='replay/test',
            event='telegram_update',
            request_id='ai-request',
            data={'group_id': 'group', 'game': '瓦'},
        )), [])

    def test_fixed_title_finishes_before_manual_title_override(self):
        config = self._config()
        config['common_event_args']['auto_upload'] = True
        config['ai_rename_args'].update({
            'fixed_game': True,
            'game': '三角洲行动',
            'update_bv_title': True,
        })
        events = LiveEvents('test', config)
        events.ended_dict['group'] = 1
        events.state_dict['group'] = [{
            'src_video': {'status': 'uploaded', 'file': None, 'wait': []},
            'src_video_pre': {'status': 'uploaded', 'file': None, 'wait': []},
            'dm_video': {'status': 'uploaded', 'file': VideoInfo(path='video.mp4'), 'wait': []},
        }]
        events.ai_rename_dict['group'] = [{
            'status': 'ready',
            'request_id': None,
            'games': ['三角洲行动'],
            'main_game': '三角洲行动',
            'renamed_types': {'dm_video'},
        }]
        events.bv_title_dict['group'] = {
            'uploads': {
                'group_dm_video_0': {
                    'bvid': 'BV1234567890',
                    'engine': 'biliuprs',
                    'title': 'original',
                    'args': {'account': 'bilibili'},
                },
            },
            'queued': {},
        }

        fixed_messages = events._check_for_bv_title('group')
        self.assertEqual(fixed_messages[0].data['phase'], 'fixed')

        self.assertEqual(events.onTelegramUpdate(PipeMessage(
            source='ai_rename',
            target='replay/test',
            event='telegram_update',
            request_id='telegram-request',
            data={'group_id': 'group', 'game': '瓦'},
        )), [])

        manual_messages = events.onBVTitleEnd(PipeMessage(
            source='uploader',
            target='replay/test',
            event='title/end',
            request_id='title-request',
            data={
                'group_id': 'group',
                'bvid': 'BV1234567890',
                'phase': 'fixed',
            },
        ))
        self.assertEqual(len(manual_messages), 1)
        self.assertEqual(manual_messages[0].data['phase'], 'manual')
        self.assertTrue(manual_messages[0].data['title'].startswith('【瓦】'))


if __name__ == '__main__':
    unittest.main()
