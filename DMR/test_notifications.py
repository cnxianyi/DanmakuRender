import logging
import unittest
from unittest.mock import patch

from DMR.notifications import TelegramNotifier, _proxy_mapping


class TelegramNotifierTests(unittest.TestCase):
    @patch('DMR.notifications.requests.post')
    def test_sends_telegram_message(self, post):
        post.return_value.raise_for_status.return_value = None
        post.return_value.json.return_value = {'ok': True, 'result': {}}
        notifier = TelegramNotifier({
            'enabled': True,
            'bot_token': 'telegram-token',
            'chat_id': '7129142702',
            'cooldown': 0,
            'machine_name': 'test-machine',
        })

        self.assertTrue(notifier.notify('直播开始', 'oyo'))
        notifier.close()
        self.assertEqual(
            post.call_args.args[0],
            'https://api.telegram.org/bottelegram-token/sendMessage',
        )
        self.assertEqual(post.call_args.kwargs['data'], {
            'chat_id': '7129142702',
            'text': '直播开始\noyo',
        })

    @patch('DMR.notifications.requests.post')
    def test_deduplicates_live_start_and_only_ends_active_live(self, post):
        post.return_value.raise_for_status.return_value = None
        post.return_value.json.return_value = {'ok': True, 'result': {}}
        notifier = TelegramNotifier({
            'enabled': True,
            'bot_token': 'telegram-token',
            'chat_id': '7129142702',
            'cooldown': 0,
        })

        self.assertTrue(notifier.live_started('oyo'))
        self.assertFalse(notifier.live_started('oyo'))
        self.assertTrue(notifier.live_ended('oyo'))
        self.assertFalse(notifier.live_ended('oyo'))
        notifier.close()
        self.assertEqual(post.call_count, 2)

    def test_builds_proxy_mapping(self):
        self.assertEqual(
            _proxy_mapping('127.0.0.1:7890'),
            {
                'http': 'http://127.0.0.1:7890',
                'https': 'http://127.0.0.1:7890',
            },
        )
        with self.assertRaises(ValueError):
            _proxy_mapping('ftp://127.0.0.1:21')

    @patch('DMR.notifications.requests.post')
    def test_logging_handler_forwards_errors(self, post):
        post.return_value.raise_for_status.return_value = None
        post.return_value.json.return_value = {'ok': True, 'result': {}}
        logger = logging.getLogger('telegram-test')
        logger.handlers.clear()
        notifier = TelegramNotifier({
            'enabled': True,
            'bot_token': 'telegram-token',
            'chat_id': '7129142702',
            'cooldown': 0,
            'machine_name': 'test-machine',
        }, logger=logger)
        notifier.install_logging_handler()
        try:
            logger.error('test error')
        finally:
            notifier.close()
        self.assertEqual(
            post.call_args.kwargs['data']['text'],
            'DanmakuRender error on test-machine\ntelegram-test: test error',
        )


if __name__ == '__main__':
    unittest.main()
