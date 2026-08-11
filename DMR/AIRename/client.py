import base64
import mimetypes
import os
import time

import requests


DEFAULT_PROMPT = """请查看这三张按视频时间顺序排列的截图，识别每张截图中的游戏。
Windows 桌面、浏览器、聊天、直播平台页面、观看其他主播直播或其他非游戏内容必须记为空字符串。
最后判断主播在该视频中主要玩的游戏；无法可靠判断时 main_game 必须为空字符串。
只返回 JSON，不要解释：
{"games":["第一张游戏名或空字符串","第二张游戏名或空字符串","第三张游戏名或空字符串"],"main_game":"主要游戏名或空字符串"}"""


class OpenAICompatibleVisionClient():
    def __init__(self, config):
        self.config = config

    def _api_key(self):
        api_key = self.config.get('api_key')
        api_key_file = self.config.get('api_key_file')
        if not api_key and api_key_file:
            try:
                with open(api_key_file, 'r', encoding='utf-8') as f:
                    api_key = f.read().strip()
            except OSError as e:
                raise ValueError(f'无法读取 AI API Key 文件 {api_key_file}: {e}')
        api_key_env = self.config.get('api_key_env', 'AI_API_KEY')
        if not api_key and api_key_env:
            api_key = os.environ.get(api_key_env)
        if not api_key:
            raise ValueError('未配置 AI API Key，请在 ai_rename_args.api_key 中填写')
        return api_key

    def _endpoint(self):
        endpoint = self.config.get('endpoint')
        if endpoint:
            return endpoint
        base_url = self.config.get('base_url', 'https://api.openai.com/v1')
        return base_url.rstrip('/') + '/chat/completions'

    def _image_content(self, frame_path):
        mime_type = mimetypes.guess_type(frame_path)[0] or 'image/jpeg'
        with open(frame_path, 'rb') as f:
            encoded = base64.b64encode(f.read()).decode('ascii')
        return {
            'type': 'image_url',
            'image_url': {
                'url': f'data:{mime_type};base64,{encoded}',
                'detail': self.config.get('image_detail', 'low'),
            },
        }

    def recognize(self, frame_paths):
        model = self.config.get('model')
        if not model:
            raise ValueError('未配置 AI 视觉模型 model')

        content = [{'type': 'text', 'text': self.config.get('prompt') or DEFAULT_PROMPT}]
        content.extend(self._image_content(path) for path in frame_paths)
        payload = {
            'model': model,
            'messages': [{'role': 'user', 'content': content}],
            'temperature': self.config.get('temperature', 0),
            'max_tokens': self.config.get('max_tokens', 300),
        }
        payload.update(self.config.get('extra_body') or {})
        if self.config.get('response_format', True):
            payload['response_format'] = {'type': 'json_object'}

        api_key = self._api_key()
        headers = {
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        }
        headers.update(self.config.get('headers') or {})

        retries = max(0, int(self.config.get('retries', 2)))
        timeout = max(1, float(self.config.get('timeout', 60)))
        last_error = None
        endpoint = self._endpoint()
        for attempt in range(retries + 1):
            try:
                response = requests.post(
                    endpoint,
                    headers=headers,
                    json=payload,
                    timeout=timeout,
                )
                response.raise_for_status()
                try:
                    body = response.json()
                except requests.exceptions.JSONDecodeError as e:
                    content_type = response.headers.get('Content-Type', 'unknown')
                    preview = ' '.join(response.text[:300].split()).replace(api_key, '***')
                    raise ValueError(
                        f'AI 接口返回了非 JSON 内容 '
                        f'(HTTP {response.status_code}, Content-Type: {content_type}): {preview!r}'
                    ) from e
                content = body['choices'][0]['message']['content']
                if isinstance(content, list):
                    content = ''.join(
                        item.get('text', '') for item in content if isinstance(item, dict)
                    )
                if not isinstance(content, str) or not content.strip():
                    raise ValueError('AI 返回内容为空')
                return content
            except (requests.RequestException, KeyError, IndexError, TypeError, ValueError) as e:
                last_error = e
                if attempt < retries:
                    time.sleep(min(2 ** attempt, 5))
        raise RuntimeError(f'AI 请求失败 ({endpoint}): {last_error}')
