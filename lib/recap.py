"""recap.py — wiki/raw/recap-*.md の発見・読み込み

ファイル名形式:
  - recap-MMDD.md (例: recap-0729.md)
  - recap-YYYY-MM-DD.md (将来形式)

直近 N 日以内の更新された recap を発見する。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

RAW_DIR = Path.home() / "wiki" / "raw"

# recap-MMDD.md (当日年を仮定するが未来なら前年)
_RE_MMDD = re.compile(r"recap-(\d{2})(\d{2})\.md$")
# recap-YYYY-MM-DD.md
_RE_YMD = re.compile(r"recap-(\d{4})-(\d{2})-(\d{2})\.md$")


def find_recaps(days: int = 7, raw_dir: Path | None = None) -> list[str]:
    """直近 ``days`` 日以内の recap ファイルパスを更新日ベースで返す。"""
    raw_dir = raw_dir or RAW_DIR
    if not raw_dir.exists():
        return []
    now = datetime.now()
    threshold = now - timedelta(days=days)
    paths: list[str] = []
    for p in sorted(raw_dir.glob("recap*.md")):
        d = _parse_recap_date(p.name, now)
        if d is None:
            continue
        if d >= threshold:
            paths.append(str(p))
    return paths


def _parse_recap_date(name: str, now: datetime) -> datetime | None:
    m = _RE_MMDD.match(name)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        try:
            d = datetime(now.year, month, day)
            if d > now:
                d = datetime(now.year - 1, month, day)
            return d
        except ValueError:
            return None
    m = _RE_YMD.match(name)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def read_recap(path: str) -> str:
    """recap ファイルを読み込む。"""
    return Path(path).read_text(encoding="utf-8")