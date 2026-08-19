import json
import re
import time

import requests


_DISCOVERED_CHAT_IDS = {}


def telegram_config(config):
    tg_config = (config or {}).get('tg')
    if isinstance(tg_config, str):
        return {'enabled': True, 'bot_token': tg_config}
    return tg_config if isinstance(tg_config, dict) else {}


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


def _send_message_once(tg_config, caption, timeout):
    token = str(tg_config.get('bot_token') or tg_config.get('token') or '').strip()
    api_base = str(tg_config.get('api_base') or 'https://api.telegram.org').rstrip('/')
    url = f'{api_base}/bot{token}/sendMessage'
    data = _request_data(tg_config)
    data['text'] = str(caption)[:4096]
    response = requests.post(
        url,
        data=data,
        timeout=timeout,
    )
    try:
        body = response.json()
    except ValueError:
        body = None
    if not response.ok or not isinstance(body, dict) or not body.get('ok'):
        description = body.get('description') if isinstance(body, dict) else response.text[:300]
        raise RuntimeError(f'HTTP {response.status_code}: {description}')
    message = body.get('result') or {}
    message_id = message.get('message_id')
    if message_id is None:
        raise RuntimeError('Telegram sendMessage 未返回 message_id')
    return {
        'chat_id': str((message.get('chat') or {}).get('id') or tg_config.get('chat_id')),
        'message_ids': [message_id],
        'media_group_id': None,
    }


def send_game_prompt(config, caption=''):
    """Send a text prompt that can be replied to with a manual game name."""
    tg_config = telegram_config(config)
    if not telegram_enabled(config):
        return None

    token = str(tg_config.get('bot_token') or tg_config.get('token') or '').strip()
    chat_id = str(tg_config.get('chat_id') or '').strip()
    if not token:
        raise ValueError('tg.bot_token（或 tg.token）未配置')
    if not chat_id:
        try:
            chat_id = _discover_chat_id(token, tg_config, max(1, float(tg_config.get('timeout', 60))))
        except Exception as error:
            raise RuntimeError(str(error).replace(token, '***')) from None
        tg_config = {**tg_config, 'chat_id': chat_id}

    timeout = max(1, float(tg_config.get('timeout', 60)))
    retries = max(0, int(tg_config.get('retries', 2)))
    retry_interval = max(0, float(tg_config.get('retry_interval', 2)))
    last_error = None
    for attempt in range(retries + 1):
        try:
            return _send_message_once(tg_config, caption, timeout)
        except (OSError, requests.RequestException, RuntimeError) as error:
            last_error = str(error).replace(token, '***')
            if attempt < retries and retry_interval:
                time.sleep(retry_interval)
    raise RuntimeError(f'Telegram 游戏名确认消息发送失败: {last_error}')


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
    )
    response.raise_for_status()
    body = response.json()
    if not body.get('ok'):
        raise RuntimeError(body.get('description') or 'getUpdates 返回失败')
    return body.get('result') or []


def parse_update_reply(update):
    """Return a replied `/update 游戏名` command, or None."""
    if not isinstance(update, dict):
        return None
    message = update.get('message') or update.get('edited_message') or {}
    text = message.get('text') or message.get('caption') or ''
    match = UPDATE_COMMAND_PATTERN.fullmatch(str(text).strip())
    reply = message.get('reply_to_message') or {}
    chat_id = (message.get('chat') or {}).get('id')
    reply_message_id = reply.get('message_id')
    if not match or chat_id is None or reply_message_id is None:
        return None
    return {
        'update_id': update.get('update_id'),
        'chat_id': str(chat_id),
        'reply_message_id': reply_message_id,
        'message_id': message.get('message_id'),
        'game': match.group(1).strip(),
    }
