import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import requests

from DMR.AIRename.bvtitle import build_bv_title, rank_games
from DMR.AIRename.client import OpenAICompatibleVisionClient
from DMR.engine import _redact_sensitive_data
from DMR.AIRename.frames import extract_frames
from DMR.AIRename.naming import rename_video
from DMR.AIRename.parser import fixed_game_result, parse_game_result
from DMR.Task.liveevents import LiveEvents
from DMR.utils import PipeMessage, VideoInfo


class AIRenameParserTests(unittest.TestCase):
    def test_parses_json_object(self):
        result = parse_game_result(
            '```json\n{"games":["三角洲行动","三角洲行动",""],'
            '"main_game":"三角洲行动"}\n```'
        )
        self.assertEqual(result['games'], ['三角洲行动', '三角洲行动', ''])
        self.assertEqual(result['main_game'], '三角洲行动')

    def test_accepts_ordered_array_and_uses_majority(self):
        result = parse_game_result('结果如下：["原神", "", "原神"]')
        self.assertEqual(result['main_game'], '原神')

    def test_non_game_names_become_empty(self):
        result = parse_game_result(
            '{"games":["Windows桌面","浏览器","抖音"],"main_game":"无法判断"}'
        )
        self.assertEqual(result, {'games': ['', '', ''], 'main_game': ''})

    def test_game_names_are_limited_to_ten_characters(self):
        result = parse_game_result(
            '{"games":["这是一个超过十个字的游戏名称"],"main_game":""}'
        )
        self.assertEqual(result['games'], ['这是一个超过十个字的'])

    def test_fixed_game_returns_three_normalized_results(self):
        result = fixed_game_result({
            'fixed_game': True,
            'game': '这是一个超过十个字的游戏名称',
        })

        self.assertEqual(result, {
            'games': ['这是一个超过十个字的'] * 3,
            'main_game': '这是一个超过十个字的',
        })

    def test_fixed_game_disabled_returns_none(self):
        self.assertIsNone(fixed_game_result({'fixed_game': False, 'game': '三角洲行动'}))


class AIRenameClientTests(unittest.TestCase):
    def test_reads_api_key_directly_from_config(self):
        client = OpenAICompatibleVisionClient({'api_key': 'yaml-key'})
        self.assertEqual(client._api_key(), 'yaml-key')

    @patch('DMR.AIRename.client.requests.post')
    def test_non_json_response_reports_http_context(self, post):
        response = post.return_value
        response.status_code = 200
        response.headers = {'Content-Type': 'text/html'}
        response.text = '<html>wrong endpoint</html>'
        response.raise_for_status.return_value = None
        response.json.side_effect = requests.exceptions.JSONDecodeError('bad json', '', 0)
        client = OpenAICompatibleVisionClient({
            'api_key': 'yaml-key',
            'model': 'vision-model',
            'endpoint': 'https://example.test/chat/completions',
            'retries': 0,
        })

        with self.assertRaisesRegex(RuntimeError, 'HTTP 200.*text/html'):
            client.recognize([])


class SensitiveLoggingTests(unittest.TestCase):
    def test_redacts_nested_api_credentials(self):
        result = _redact_sensitive_data({
            'args': {
                'api_key': 'secret',
                'headers': {'Authorization': 'Bearer secret'},
                'api_key_env': 'AI_API_KEY',
            },
        })

        self.assertEqual(result['args']['api_key'], '***')
        self.assertEqual(result['args']['headers']['Authorization'], '***')
        self.assertEqual(result['args']['api_key_env'], 'AI_API_KEY')


class BVTitleTests(unittest.TestCase):
    def test_ranks_all_segment_frame_results_by_count(self):
        states = [
            {'games': ['三角洲行动', '三角洲行动', '幻兽帕鲁']},
            {'games': ['幻兽帕鲁', '三角洲行动', '']},
            {'games': ['三角洲行动', '幻兽帕鲁', '三角洲行动']},
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

    def test_title_uses_at_most_two_games_and_truncates_old_cache_names(self):
        states = [{
            'games': [
                '这是一个超过十个字的游戏名称',
                '三角洲行动',
                '幻兽帕鲁',
            ],
        }]

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


class AIRenameFrameTests(unittest.TestCase):
    @patch('DMR.AIRename.frames.FFprobe.get_duration', return_value=3.0)
    @patch('DMR.AIRename.frames.ToolsList.get', return_value='ffmpeg')
    def test_last_frame_retries_one_second_earlier(self, _get_tool, _get_duration):
        with tempfile.TemporaryDirectory() as directory:
            video_path = os.path.join(directory, 'video.mp4')
            frame_path = os.path.join(directory, 'frame.jpg')
            open(video_path, 'wb').close()
            timestamps = []

            def fake_run(command, **kwargs):
                timestamps.append(command[command.index('-ss') + 1])
                if len(timestamps) == 2:
                    with open(frame_path, 'wb') as f:
                        f.write(b'frame')
                    return SimpleNamespace(returncode=0, stderr=b'')
                return SimpleNamespace(returncode=1, stderr=b'no frame')

            with patch('DMR.AIRename.frames.get_tempfile', return_value=frame_path), \
                    patch('DMR.AIRename.frames.subprocess.run', side_effect=fake_run):
                frames = extract_frames(
                    VideoInfo(path=video_path, duration=3),
                    {'frame_positions': [0.99]},
                )

            self.assertEqual(frames, [frame_path])
            self.assertEqual(timestamps, ['2.970', '2.000'])


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

    def test_segment_queues_ai_and_blocks_upload_until_callback(self):
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

        self.assertEqual(messages[0].target, 'ai_rename')
        self.assertFalse(events._ai_rename_allows_upload('group', 0, 'src_video'))

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
        self.assertEqual(events.ai_rename_dict['group'][0]['games'], ['三角洲行动'] * 3)

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


if __name__ == '__main__':
    unittest.main()
