import subprocess
import unittest
from unittest.mock import Mock, patch

from DMR.Downloader.ffmpeg import FFmpegDownloader
from DMR.Downloader.stream_downloader import StreamDownloadTask


class StreamURLRecoveryTests(unittest.TestCase):
    def _task(self, **advanced_video_args):
        task = StreamDownloadTask.__new__(StreamDownloadTask)
        task.advanced_video_args = {
            'stream_url_retries': 3,
            'stream_url_retry_interval': 0,
            **advanced_video_args,
        }
        task.stream_option = {}
        task.liveapi = Mock()
        task.taskname = 'test'
        task.logger = Mock()
        return task

    def test_retries_empty_stream_url_before_returning_valid_url(self):
        task = self._task()
        task.liveapi.GetStreamURL.side_effect = [None, '', 'https://example.test/live.flv']

        self.assertEqual(task._get_stream_url(), 'https://example.test/live.flv')
        self.assertEqual(task.liveapi.GetStreamURL.call_count, 3)

    def test_rejects_stream_url_after_bounded_retries(self):
        task = self._task()
        task.liveapi.GetStreamURL.return_value = None

        with self.assertRaisesRegex(RuntimeError, '连续 3 次未获取到有效直播流地址'):
            task._get_stream_url()

        self.assertEqual(task.liveapi.GetStreamURL.call_count, 3)


class StreamStateRecoveryTests(unittest.TestCase):
    def _task(self):
        task = StreamDownloadTask.__new__(StreamDownloadTask)
        task.taskname = 'test'
        task.stop_wait_time = 0
        task.advanced_video_args = {
            'restart_interval': [0, 0, 0],
            'restart_reset_after': 300,
            'start_check_interval': 0,
            'stop_check_interval': 0,
        }
        task.liveapi = Mock()
        task.logger = Mock()
        task._pipeSend = Mock()
        task.stop_once = Mock()
        return task

    def test_unknown_onair_state_does_not_emit_live_end(self):
        task = self._task()
        task.liveapi.Onair.return_value = None

        def stop_loop(_interval):
            task.loop = False

        with patch('DMR.Downloader.stream_downloader.time.sleep', side_effect=stop_loop):
            task.start_helper()

        task._pipeSend.assert_not_called()

    def test_reconnect_does_not_emit_duplicate_live_start(self):
        task = self._task()
        task.liveapi.Onair.side_effect = [True, True, True]
        attempts = 0

        def start_once():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError('temporary failure')
            task.loop = False

        task.start_once = start_once
        task.start_helper()

        events = [call.args[0] for call in task._pipeSend.call_args_list]
        self.assertEqual(events.count('livestart'), 1)
        self.assertEqual(events.count('liveerror'), 1)
        self.assertEqual(attempts, 2)


class FFmpegLifecycleTests(unittest.TestCase):
    def _downloader(self):
        return FFmpegDownloader(
            stream_url='https://example.test/live.flv',
            output_dir='/tmp',
            output_format='flv',
            taskname='test',
            ffmpeg='ffmpeg',
            segment_callback=Mock(),
            stable_callback=Mock(),
        )

    def test_stop_before_process_start_is_idempotent(self):
        downloader = self._downloader()

        downloader.stop()
        downloader.stop()

        self.assertTrue(downloader.stoped)
        self.assertIsNone(downloader.ffmpeg_proc)

    @patch('DMR.Downloader.ffmpeg.subprocess.Popen', side_effect=OSError('popen failed'))
    def test_popen_failure_does_not_break_cleanup(self, _popen):
        downloader = self._downloader()

        with self.assertRaisesRegex(OSError, 'popen failed'):
            downloader.start()

        downloader.stop()
        self.assertIsNone(downloader.ffmpeg_proc)

    def test_stop_kills_process_after_graceful_timeout(self):
        downloader = self._downloader()
        process = Mock()
        process.poll.return_value = None
        process.stdin = Mock()
        process.wait.side_effect = [
            subprocess.TimeoutExpired(cmd='ffmpeg', timeout=3),
            0,
        ]
        downloader.ffmpeg_proc = process

        downloader.stop()

        process.kill.assert_called_once_with()
        self.assertEqual(process.wait.call_count, 2)

    def test_exit_error_contains_return_code_and_recent_reason(self):
        downloader = self._downloader()
        process = Mock()
        process.poll.side_effect = [None, 1]

        def start_ffmpeg():
            downloader.ffmpeg_proc = process
            downloader.msg_queue.put('Error during demuxing: Connection timed out')

        with patch.object(downloader, 'start_ffmpeg', side_effect=start_ffmpeg):
            with self.assertRaisesRegex(
                RuntimeError,
                '返回码 1.*Connection timed out',
            ):
                downloader.start()


if __name__ == '__main__':
    unittest.main()
