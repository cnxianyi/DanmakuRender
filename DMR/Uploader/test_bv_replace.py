import os
import tempfile
import unittest
from unittest.mock import Mock

from DMR.Uploader.biliwebapi import BiliWebApi, Data


def archive(parts):
    data = Data(bvid='BV1nE8Z63EEy', title='archive', desc='description')
    data.videos = [dict(part) for part in parts]
    return data


class BiliWebApiReplaceTests(unittest.TestCase):
    def api(self):
        api = object.__new__(BiliWebApi)
        api.sort_videos = True
        api.upload_part = Mock(return_value=(True, {
            'title': 'temporary-name',
            'filename': 'new-upload-filename',
            'desc': '',
        }))
        api.submit = Mock(return_value={'code': 0, 'data': {'bvid': 'BV1nE8Z63EEy'}})
        return api

    def test_replaces_only_requested_part_and_preserves_title(self):
        parts = [
            {'title': 'P1-title', 'filename': 'old-1', 'cid': 101, 'desc': 'p1'},
            {'title': 'P2-title', 'filename': 'old-2', 'cid': 102, 'desc': 'p2'},
            {'title': 'P3-title', 'filename': 'old-3', 'cid': 103, 'desc': 'p3'},
            {'title': 'P4-title', 'filename': 'old-4', 'cid': 104, 'desc': 'p4'},
        ]
        before = archive(parts)
        latest = archive(parts)
        verified_parts = [dict(part) for part in parts]
        verified_parts[2] = {
            'title': 'P3-title',
            'filename': 'new-upload-filename',
            'cid': 999,
            'desc': 'p3',
        }
        api = self.api()
        api.get_remote_data = Mock(side_effect=[before, latest, archive(verified_parts)])

        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, 'clip.mp4')
            with open(source, 'wb') as file:
                file.write(b'clip')
            result = api.replace_video_part(
                'BV1nE8Z63EEy',
                3,
                source,
                expected_cid=103,
                expected_title='P3-title',
                verify_interval=0,
            )

        submitted = api.submit.call_args.kwargs['videos']
        self.assertEqual(submitted.videos[0], parts[0])
        self.assertEqual(submitted.videos[1], parts[1])
        self.assertEqual(submitted.videos[3], parts[3])
        self.assertEqual(submitted.videos[2], {
            'title': 'P3-title',
            'filename': 'new-upload-filename',
            'desc': 'p3',
        })
        self.assertTrue(result['verified'])
        self.assertTrue(api.sort_videos)

    def test_aborts_if_part_changes_during_upload(self):
        initial_parts = [
            {'title': 'P1-title', 'filename': 'old-1', 'cid': 101, 'desc': ''},
            {'title': 'P2-title', 'filename': 'old-2', 'cid': 102, 'desc': ''},
        ]
        changed_parts = [
            initial_parts[0],
            {'title': 'different', 'filename': 'old-2b', 'cid': 202, 'desc': ''},
        ]
        api = self.api()
        api.get_remote_data = Mock(side_effect=[archive(initial_parts), archive(changed_parts)])

        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, 'clip.mp4')
            open(source, 'wb').close()
            with self.assertRaisesRegex(RuntimeError, '上传期间发生变化'):
                api.replace_video_part(
                    'BV1nE8Z63EEy',
                    2,
                    source,
                    expected_cid=102,
                    expected_title='P2-title',
                    verify_interval=0,
                )

        api.submit.assert_not_called()


if __name__ == '__main__':
    unittest.main()
