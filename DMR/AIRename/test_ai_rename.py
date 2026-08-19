import os
import tempfile
import unittest
from unittest.mock import patch

from DMR.AIRename.bvtitle import build_bv_title, rank_games
from DMR import _redact_sensitive_config
from DMR.engine import _redact_sensitive_data
from DMR.AIRename.naming import rename_video
from DMR.AIRename.parser import fixed_game_result
from DMR.AIRename.telegram import (
    parse_update_reply,
    send_game_prompt,
    telegram_config,
)
from DMR.Task.liveevents import LiveEvents
from DMR.utils import PipeMessage, VideoInfo


class GameNameParserTests(unittest.TestCase):
    def test_fixed_game_returns_three_normalized_results(self):
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
        result = _redact_sensitive_data({
            'args': {
                'tg': {'bot_token': 'telegram-secret'},
            },
        })

        self.assertEqual(result['args']['tg']['bot_token'], '***')

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

    def test_parses_update_only_when_replying_to_notification(self):
        command = parse_update_reply({
            'update_id': 88,
            'message': {
                'message_id': 200,
                'chat': {'id': 123456},
                'text': '/update 瓦',
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
        self.assertIsNone(parse_update_reply({
            'message': {
                'chat': {'id': 123456},
                'text': '/update 瓦',
            },
        }))

    @patch('DMR.AIRename.telegram.requests.post')
    def test_sends_text_prompt_and_returns_message_id(self, post):
        post.return_value.ok = True
        post.return_value.json.return_value = {
            'ok': True,
            'result': {
                'message_id': 301,
                'chat': {'id': 123456},
            },
        }

        sent = send_game_prompt({
            'tg': {
                'enabled': True,
                'bot_token': 'telegram-token',
                'chat_id': '123456',
                'retries': 0,
            },
        }, caption='reply with /update game')

        self.assertEqual(sent, {
            'chat_id': '123456',
            'message_ids': [301],
            'media_group_id': None,
        })
        self.assertTrue(post.call_args.args[0].endswith('/bottelegram-token/sendMessage'))
        self.assertEqual(post.call_args.kwargs['data']['text'], 'reply with /update game')


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

    def test_fixed_game_with_telegram_queues_text_prompt(self):
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

        ai_messages = [message for message in messages if message.target == 'ai_rename']
        self.assertEqual(len(ai_messages), 1)
        self.assertEqual(
            ai_messages[0].data['args']['_fixed_result']['main_game'],
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


if __name__ == '__main__':
    unittest.main()
