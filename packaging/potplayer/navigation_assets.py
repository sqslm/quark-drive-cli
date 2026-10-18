"""Give each root category a same-stem icon without duplicating source logic."""
from pathlib import Path

ENTRIES = (
    ('drive', '', 'cloud'), ('search', 'Search', 'search'),
    ('favorites', 'Favorites', 'folder-heart'), ('recent', 'Recent', 'history'),
    ('continue', 'Continue', 'clapperboard'), ('highlights', 'Highlights', 'bookmark'),
    ('diagnostics', 'Manager', 'settings'),
)
SELECTOR = 'const string QUARK_ROOT_CATEGORY = "";'


def stage_navigation(script: Path, destination: Path) -> list[Path]:
    """PotPlayer chooses the root icon per script, rather than per category."""
    source = script.read_text('utf-8-sig')
    if SELECTOR not in source:
        raise ValueError('导航源缺少分类选择器')
    written = []
    for category, suffix, icon in ENTRIES:
        stem = script.stem + (' - ' + suffix if suffix else '')
        target = destination / (stem + '.as')
        target.write_text(source.replace(SELECTOR, f'const string QUARK_ROOT_CATEGORY = "{category}";'),
                          encoding='utf-8-sig')
        target.with_suffix('.ico').write_bytes((script.parent / 'icons' / (icon + '.ico')).read_bytes())
        written.append(target)
    return written
