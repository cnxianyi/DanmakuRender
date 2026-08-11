import json
import re


DEFAULT_NON_GAME_NAMES = {
    'none', 'null', 'unknown', 'windows', 'windows桌面', '桌面', '浏览器',
    '抖音', '直播', '非游戏', '无', '未知', '无法判断', '无法识别',
}


def _clean_json_text(text):
    text = text.strip()
    fenced = re.fullmatch(r'```(?:json)?\s*(.*?)\s*```', text, flags=re.I | re.S)
    if fenced:
        text = fenced.group(1)
    if not text.startswith(('{', '[')):
        obj_start = text.find('{')
        arr_start = text.find('[')
        starts = [start for start in (obj_start, arr_start) if start >= 0]
        if starts:
            start = min(starts)
            end_char = '}' if text[start] == '{' else ']'
            end = text.rfind(end_char)
            if end > start:
                text = text[start:end + 1]
    return text.strip()


def _normalize_game(value, config):
    if not isinstance(value, str):
        return ''
    value = re.sub(r'\s+', ' ', value).strip().strip('"\'`[]【】')
    excluded = {str(x).strip().lower() for x in (config.get('excluded_names') or [])}
    excluded.update(DEFAULT_NON_GAME_NAMES)
    if not value or value.lower() in excluded:
        return ''
    max_length = max(1, int(config.get('max_game_name_length', 10)))
    return value[:max_length]


def _majority(games):
    counts = {}
    for game in games:
        if game:
            counts[game] = counts.get(game, 0) + 1
    if not counts:
        return ''
    best_count = max(counts.values())
    for game in games:
        if game and counts[game] == best_count:
            return game
    return ''


def fixed_game_result(config=None):
    config = config or {}
    if not config.get('fixed_game'):
        return None

    game = _normalize_game(config.get('game', ''), config)
    positions = config.get('frame_positions', [0.33, 0.66, 0.99])
    frame_count = len(positions) if isinstance(positions, (list, tuple)) and positions else 3
    return {
        'games': [game] * frame_count,
        'main_game': game,
    }


def parse_game_result(text, config=None):
    config = config or {}
    data = json.loads(_clean_json_text(text))
    if isinstance(data, list):
        raw_games = data
        raw_main_game = ''
    elif isinstance(data, dict):
        raw_games = data.get('games', [])
        raw_main_game = data.get('main_game', '')
    else:
        raise ValueError('AI 返回值必须是 JSON 对象或数组')

    if not isinstance(raw_games, list):
        raise ValueError('AI 返回的 games 必须是数组')
    games = [_normalize_game(game, config) for game in raw_games]
    main_game = _normalize_game(raw_main_game, config) or _majority(games)
    return {'games': games, 'main_game': main_game}
