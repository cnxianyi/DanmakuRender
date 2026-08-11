from collections import Counter


def rank_games(ai_states, source='games', min_count=1, max_games=2, max_name_length=10):
    """Return game names ordered by frequency, then first appearance."""
    counts = Counter()
    first_seen = {}

    for state in ai_states:
        values = state.get('games') if source == 'games' else [state.get('main_game')]
        if not values:
            values = [state.get('main_game')]
        for value in values:
            game = str(value or '').strip()[:max(1, int(max_name_length or 10))]
            if not game:
                continue
            if game not in first_seen:
                first_seen[game] = len(first_seen)
            counts[game] += 1

    min_count = max(1, int(min_count or 1))
    games = [game for game, count in counts.items() if count >= min_count]
    games.sort(key=lambda game: (-counts[game], first_seen[game]))

    max_games = int(max_games or 0)
    if max_games > 0:
        games = games[:max_games]
    return games


def build_bv_title(base_title, ai_states, config):
    base_title = str(base_title or '').strip()
    if not base_title:
        return '', []

    games = rank_games(
        ai_states,
        source=config.get('bv_title_count_source', 'games'),
        min_count=config.get('bv_title_min_count', 1),
        max_games=config.get('bv_title_max_games', 2),
        max_name_length=config.get('max_game_name_length', 10),
    )
    if not games:
        return '', []

    game_text = str(config.get('bv_title_separator', '|')).join(games)
    template = config.get('bv_title_template', '【{GAMES}】{TITLE}')
    title = template.replace('{GAMES}', game_text).replace('{TITLE}', base_title)
    max_length = max(1, int(config.get('bv_title_max_length', 80)))
    return title[:max_length], games


def update_bilibili_title(bvid, title, upload_args):
    from DMR.Uploader.biliwebapi import BiliWebApi

    uploader = BiliWebApi(
        cookies=upload_args.get('cookies'),
        account=upload_args.get('account'),
        limit=upload_args.get('limit', 3),
    )
    try:
        videos = uploader.get_remote_data(bvid)
        if videos is None:
            raise RuntimeError(f'无法获取 B 站稿件 {bvid}')
        if videos.title == title:
            return False
        videos.title = title
        uploader.submit(submit_api='web', videos=videos)
        return True
    finally:
        uploader.stop()
