import re


DEFAULT_NON_GAME_NAMES = {
    'none', 'null', 'unknown', 'windows', 'windows桌面', '桌面', '浏览器',
    '抖音', '直播', '非游戏', '无', '未知', '无法判断', '无法识别',
}


def normalize_game(value, config=None):
    config = config or {}
    if not isinstance(value, str):
        return ''
    value = re.sub(r'\s+', ' ', value).strip().strip('"\'`[]【】')
    excluded = {str(x).strip().lower() for x in (config.get('excluded_names') or [])}
    excluded.update(DEFAULT_NON_GAME_NAMES)
    if not value or value.lower() in excluded:
        return ''
    max_length = max(1, int(config.get('max_game_name_length', 10)))
    return value[:max_length]


def fixed_game_result(config=None):
    config = config or {}
    if not config.get('fixed_game'):
        return None

    game = normalize_game(config.get('game', ''), config)
    return {
        'games': [game] if game else [],
        'main_game': game,
    }
