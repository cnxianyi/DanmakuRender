import os
import re

from DMR.utils import rename_safe, replace_invalid_chars


def rename_video(video, game, config):
    game = replace_invalid_chars(str(game or '')).strip()
    if not game:
        return video.path

    source_path = video.path
    directory = os.path.dirname(source_path)
    filename = os.path.basename(source_path)
    stem, extension = os.path.splitext(filename)
    prefix_pattern = config.get('existing_prefix_pattern', r'^【[^】]+】')
    if re.match(prefix_pattern, stem):
        return source_path

    template = config.get('rename_template', '【{GAME}】{BASENAME}')
    new_stem = template.format(GAME=game, BASENAME=stem)
    new_stem = replace_invalid_chars(new_stem)
    destination = os.path.join(directory, new_stem + extension)
    renamed_path = rename_safe(source_path, destination)
    if not renamed_path:
        raise OSError(f'无法重命名视频: {source_path} -> {destination}')
    video.path = renamed_path
    return renamed_path
