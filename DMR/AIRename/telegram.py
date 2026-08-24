import json
import mimetypes
import os
import re
import time
from contextlib import ExitStack
from urllib.parse import urlsplit

import requests


_DISCOVERED_CHAT_IDS = {}


def telegram_config(config):
    tg_config = (config or {}).get('tg')
    if isinstance(tg_config, str):
        return {'enabled': True, 'bot_token': tg_config}
    return tg_config if isinstance(tg_config, dict) else {}


def telegram_proxies(tg_config):
    """Return the requests proxy mapping configured for Telegram API calls."""
    proxy = (tg_config or {}).get('proxy')
    if not proxy:
        return None

    proxy = str(proxy).strip()
    if not proxy:
        return None
    if '://' not in proxy:
        proxy = f'http://{proxy}'

    try:
        parsed = urlsplit(proxy)
    except ValueError as error:
        raise ValueError(
            'tg.proxy 必须是 http://、https://、socks5:// 或 socks5h:// 代理地址'
        ) from error

    if parsed.scheme.lower() not in {'http', 'https', 'socks5', 'socks5h'} or not parsed.netloc:
        raise ValueError(
            'tg.proxy 必须是 http://、https://、socks5:// 或 socks5h:// 代理地址'
        )
    return {'http': proxy, 'https': proxy}


def telegram_enabled(config):
    tg_config = telegram_config(config)
    return bool(tg_config.get('enabled'))


UPDATE_COMMAND_PATTERN = re.compile(
    r'^/update(?:@[A-Za-z0-9_]+)?\s+(.+?)\s*$',
    flags=re.I | re.S,
)


def _discover_chat_id(token, tg_config, timeout):
    api_base = str(tg_config.get('api_base') or 'https://api.telegram.org').rstrip('/')
    cache_key = (api_base, token)
    if cache_key in _DISCOVERED_CHAT_IDS:
        return _DISCOVERED_CHAT_IDS[cache_key]
    response = requests.get(
        f'{api_base}/bot{token}/getUpdates',
        params={'limit': 100, 'timeout': 0},
        timeout=timeout,
        proxies=telegram_proxies(tg_config),
    )
    response.raise_for_status()
    body = response.json()
    if not body.get('ok'):
        raise RuntimeError(body.get('description') or 'getUpdates 返回失败')

    chat_ids = []
    for update in body.get('result') or []:
        for key in ('message', 'edited_message', 'channel_post', 'edited_channel_post'):
            chat_id = ((update.get(key) or {}).get('chat') or {}).get('id')
            if chat_id is not None and str(chat_id) not in chat_ids:
                chat_ids.append(str(chat_id))
    if not chat_ids:
        raise ValueError('tg.chat_id 未配置；请先向机器人发送 /start，或手动填写 chat_id')
    if len(chat_ids) > 1:
        raise ValueError(f'tg.chat_id 未配置，且最近更新中有多个聊天: {chat_ids}')
    _DISCOVERED_CHAT_IDS[cache_key] = chat_ids[0]
    return chat_ids[0]


def _request_data(tg_config):
    data = {
        'chat_id': str(tg_config.get('chat_id') or '').strip(),
    }
    message_thread_id = tg_config.get('message_thread_id')
    if message_thread_id not in (None, ''):
        data['message_thread_id'] = str(message_thread_id)
    if tg_config.get('disable_notification'):
        data['disable_notification'] = 'true'
    return data


def _post_album(frame_paths, tg_config, caption, timeout):
    token = str(tg_config.get('bot_token') or tg_config.get('token') or '').strip()
    api_base = str(tg_config.get('api_base') or 'https://api.telegram.org').rstrip('/')
    data = _request_data(tg_config)
    media = []

    with ExitStack() as stack:
        files = {}
        for index, frame_path in enumerate(frame_paths):
            field = f'frame{index}'
            item = {'type': 'photo', 'media': f'attach://{field}'}
            if index == 0 and caption:
                item['caption'] = str(caption)[:1024]
            media.append(item)
            mime_type = mimetypes.guess_type(frame_path)[0] or 'image/jpeg'
            file_handle = stack.enter_context(open(frame_path, 'rb'))
            files[field] = (os.path.basename(frame_path), file_handle, mime_type)
        data['media'] = json.dumps(media, ensure_ascii=False)
        return requests.post(
            f'{api_base}/bot{token}/sendMediaGroup',
            data=data,
            files=files,
            timeout=timeout,
            proxies=telegram_proxies(tg_config),
        )


def _post_photo(frame_path, tg_config, caption, timeout):
    token = str(tg_config.get('bot_token') or tg_config.get('token') or '').strip()
    api_base = str(tg_config.get('api_base') or 'https://api.telegram.org').rstrip('/')
    data = _request_data(tg_config)
    if caption:
        data['caption'] = str(caption)[:1024]
    mime_type = mimetypes.guess_type(frame_path)[0] or 'image/jpeg'
    with open(frame_path, 'rb') as file_handle:
        return requests.post(
            f'{api_base}/bot{token}/sendPhoto',
            data=data,
            files={'photo': (os.path.basename(frame_path), file_handle, mime_type)},
            timeout=timeout,
            proxies=telegram_proxies(tg_config),
        )


def _send_frame_batch(frame_paths, tg_config, caption, timeout):
    if len(frame_paths) == 1:
        response = _post_photo(frame_paths[0], tg_config, caption, timeout)
    else:
        response = _post_album(frame_paths, tg_config, caption, timeout)
    try:
        body = response.json()
    except ValueError:
        body = None
    if not response.ok or not isinstance(body, dict) or not body.get('ok'):
        description = body.get('description') if isinstance(body, dict) else response.text[:300]
        raise RuntimeError(f'HTTP {response.status_code}: {description}')

    messages = body.get('result') or []
    if isinstance(messages, dict):
        messages = [messages]
    message_ids = [
        message.get('message_id')
        for message in messages
        if isinstance(message, dict) and message.get('message_id') is not None
    ]
    if len(message_ids) != len(frame_paths):
        raise RuntimeError('Telegram 图片发送成功，但返回的消息数量不完整')
    media_group_id = next((
        message.get('media_group_id')
        for message in messages
        if isinstance(message, dict) and message.get('media_group_id')
    ), None)
    result_chat_id = next((
        (message.get('chat') or {}).get('id')
        for message in messages
        if isinstance(message, dict) and (message.get('chat') or {}).get('id') is not None
    ), tg_config.get('chat_id'))
    return {
        'chat_id': str(result_chat_id),
        'message_ids': message_ids,
        'media_group_id': media_group_id,
    }


def send_frame_album(frame_paths, config, caption=''):
    """Send screenshots as Telegram media groups and return all reply targets."""
    tg_config = telegram_config(config)
    if not telegram_enabled(config):
        return None

    frame_paths = list(frame_paths or [])
    token = str(tg_config.get('bot_token') or tg_config.get('token') or '').strip()
    chat_id = str(tg_config.get('chat_id') or '').strip()
    if not token:
        raise ValueError('tg.bot_token（或 tg.token）未配置')
    if not frame_paths:
        raise ValueError('没有可发送的截图')
    if any(not os.path.isfile(path) for path in frame_paths):
        raise FileNotFoundError('待发送的截图文件不存在')

    timeout = max(1, float(tg_config.get('timeout', 60)))
    if not chat_id:
        try:
            chat_id = _discover_chat_id(token, tg_config, timeout)
        except Exception as error:
            raise RuntimeError(str(error).replace(token, '***')) from None
        tg_config = {**tg_config, 'chat_id': chat_id}
    retries = max(0, int(tg_config.get('retries', 2)))
    retry_interval = max(0, float(tg_config.get('retry_interval', 2)))
    last_error = None
    message_ids = []
    media_group_ids = []
    result_chat_id = chat_id

    # Telegram limits one media group to 10 items. A long live is split into
    # consecutive groups while replies to every returned message share one target.
    batches = [frame_paths[index:index + 10] for index in range(0, len(frame_paths), 10)]
    for batch_index, batch in enumerate(batches):
        for attempt in range(retries + 1):
            try:
                result = _send_frame_batch(
                    batch,
                    tg_config,
                    caption if batch_index == 0 else '',
                    timeout,
                )
                result_chat_id = result['chat_id']
                message_ids.extend(result['message_ids'])
                if result.get('media_group_id'):
                    media_group_ids.append(result['media_group_id'])
                break
            except (OSError, requests.RequestException, RuntimeError) as error:
                last_error = str(error).replace(token, '***')
                if attempt < retries and retry_interval:
                    time.sleep(retry_interval)
        else:
            raise RuntimeError(f'Telegram 图片发送失败: {last_error}')

    return {
        'chat_id': str(result_chat_id),
        'message_ids': message_ids,
        'media_group_id': media_group_ids[0] if media_group_ids else None,
        'media_group_ids': media_group_ids,
    }


def get_updates(config, offset=None):
    """Long-poll Telegram updates for manual title overrides."""
    tg_config = telegram_config(config)
    token = str(tg_config.get('bot_token') or tg_config.get('token') or '').strip()
    if not token:
        raise ValueError('tg.bot_token（或 tg.token）未配置')

    api_base = str(tg_config.get('api_base') or 'https://api.telegram.org').rstrip('/')
    poll_timeout = max(0, int(tg_config.get('poll_timeout', 20)))
    request_timeout = max(
        float(tg_config.get('timeout', 60)),
        poll_timeout + 5,
    )
    params = {
        'limit': 100,
        'timeout': poll_timeout,
        'allowed_updates': json.dumps(['message', 'edited_message']),
    }
    if offset is not None:
        params['offset'] = int(offset)

    response = requests.get(
        f'{api_base}/bot{token}/getUpdates',
        params=params,
        timeout=request_timeout,
        proxies=telegram_proxies(tg_config),
    )
    response.raise_for_status()
    body = response.json()
    if not body.get('ok'):
        raise RuntimeError(body.get('description') or 'getUpdates 返回失败')
    return body.get('result') or []


def parse_update_reply(update):
    """Return a game name from a reply to a tracked Telegram screenshot."""
    if not isinstance(update, dict):
        return None
    message = update.get('message') or update.get('edited_message') or {}
    text = str(message.get('text') or message.get('caption') or '').strip()
    match = UPDATE_COMMAND_PATTERN.fullmatch(text)
    if match:
        game = match.group(1).strip()
    elif text and not text.startswith('/'):
        game = text
    else:
        game = ''
    reply = message.get('reply_to_message') or {}
    chat_id = (message.get('chat') or {}).get('id')
    reply_message_id = reply.get('message_id')
    if not game or chat_id is None or reply_message_id is None:
        return None
    return {
        'update_id': update.get('update_id'),
        'chat_id': str(chat_id),
        'reply_message_id': reply_message_id,
        'message_id': message.get('message_id'),
        'game': game,
    }
