"""Identity-safe lifecycle for generated wiki entity pages."""

from __future__ import annotations

import hashlib
import json
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .fsio import write_text_atomic

NOTES_START = "<!-- tca:notes:start -->"
NOTES_END = "<!-- tca:notes:end -->"
# `va wiki` 재생성이 남기는 엔티티 recall 기록 — 감사가 읽는다.
ENTITY_RECALL_FILENAME: Final = ".tca-entity-recall.json"

NOTES_PLACEHOLDER = (
    "\n(에이전트 서술 구역 — 재생성해도 보존된다. 다각 관점 종합·"
    "모순 플래그를 여기에 기록)\n"
)
_NOTES_RE = re.compile(
    re.escape(NOTES_START) + r"(.*?)" + re.escape(NOTES_END), re.DOTALL
)
_ENTITY_RE = re.compile(r"^entity:\s*(.+)$", re.MULTILINE)
_GENERATED_ENTITY_TYPE = "type: tca-wiki-entity"
_QUARANTINED_ENTITY_TYPE = "type: tca-wiki-hypothesis"
# `%`까지 escape해야 되돌릴 수 있다 — 그러지 않으면 `A%5BB`라는 라벨이
# `A[B`의 슬러그를 그대로 차지한다.
_RESERVED_RE = re.compile(r'[\\/:*?"<>|#\[\]^\n%]')
_SLUG_MAX: Final = 80


@dataclass(frozen=True, slots=True)
class EntityPagePlan:
    paths: dict[str, Path]
    notes_sources: dict[str, Path | None]
    stale: tuple[Path, ...]
    restored: tuple[Path, ...]


def _require_entity_directory(path: Path, *, missing_ok: bool = False) -> bool:
    """Reject symlink/non-directory projection roots before any traversal."""
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        if missing_ok:
            return False
        raise
    if not stat.S_ISDIR(mode):
        raise OSError(f"unsafe wiki entity directory (not a directory): {path}")
    return True


def _require_entity_page(path: Path) -> None:
    """Only locally owned regular files may participate in entity lifecycle."""
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        raise
    if not stat.S_ISREG(mode):
        raise OSError(f"unsafe wiki entity page (not a regular file): {path}")


def _entity_pages(path: Path, *, missing_ok: bool = False) -> list[Path]:
    if not _require_entity_directory(path, missing_ok=missing_ok):
        return []
    pages = sorted(path.glob("*.md"))
    for page in pages:
        _require_entity_page(page)
    return pages


def _ensure_entity_directory(parent: Path, name: str) -> Path:
    _require_entity_directory(parent)
    child = parent / name
    if not _require_entity_directory(child, missing_ok=True):
        child.mkdir()
    return child


def _notes_from_text(text: str) -> str:
    match = _NOTES_RE.search(text)
    return match.group(1) if match else NOTES_PLACEHOLDER


def existing_notes(path: Path | None) -> str:
    """Return the preserved agent notes block for one generated entity page."""
    if path is None:
        return NOTES_PLACEHOLDER
    _require_entity_page(path)
    return _notes_from_text(path.read_text(encoding="utf-8"))


def _label_digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()[:8]


def _slug(label: str) -> str:
    """서로 다른 라벨에 서로 다른 슬러그를 준다 — 라벨만 보고.

    예약 문자를 하나의 `_`로 뭉개면 `A[B`와 `A#B`가 같은 슬러그를 두고
    다투게 되고, 승자는 디스크에 무엇이 먼저 있었는지로 갈렸다. 그래서 같은
    원장을 순차로 쌓느냐 한 번에 재생성하느냐에 따라 골격이 뒤바뀌었다.
    문자마다 다른 escape를 쓰면 그 다툼 자체가 생기지 않는다.

    길이 상한은 정보를 잃으므로 잘린 자리에 라벨 지문을 남긴다.
    """
    slug = _RESERVED_RE.sub(lambda m: f"%{ord(m.group()):02X}", label).strip()
    if len(slug) > _SLUG_MAX:
        slug = f"{slug[:_SLUG_MAX - 9]}-{_label_digest(label)}"
    return slug or "_"


def _generated_entity(path: Path, page_type: str) -> str | None:
    _require_entity_page(path)
    text = path.read_text(encoding="utf-8")
    frontmatter = text.split("---", 2)
    if len(frontmatter) < 3 or page_type not in frontmatter[1]:
        return None
    match = _ENTITY_RE.search(text)
    if match is None:
        return None
    try:
        label = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return label if isinstance(label, str) and label else None


def plan_entity_pages(ent_dir: Path, labels: set[str]) -> EntityPagePlan:
    """Keep paths stable by entity identity while allocating collision-safe slugs."""
    existing: dict[str, Path] = {}
    stale: list[Path] = []
    active_pages = _entity_pages(ent_dir)
    for path in active_pages:
        label = _generated_entity(path, _GENERATED_ENTITY_TYPE)
        if label is None:
            continue
        if label in existing:
            stale.append(path)
        else:
            existing[label] = path
    wiki = ent_dir.parent
    _require_entity_directory(wiki)
    hypotheses = wiki / "hypotheses"
    if _require_entity_directory(hypotheses, missing_ok=True):
        quarantine_pages = _entity_pages(
            hypotheses / "entities",
            missing_ok=True,
        )
    else:
        quarantine_pages = []
    quarantined: dict[str, list[Path]] = {}
    for path in quarantine_pages:
        label = _generated_entity(path, _QUARANTINED_ENTITY_TYPE)
        if label is not None:
            quarantined.setdefault(label, []).append(path)
    used = {path.stem.casefold() for path in active_pages}
    paths: dict[str, Path] = {}
    notes_sources: dict[str, Path | None] = {}
    restored: list[Path] = []
    for label in sorted(labels):
        if label in existing:
            path = existing.pop(label)
            paths[label] = path
            notes_sources[label] = path
            source = path
        else:
            sources = quarantined.get(label, [])
            source = sources[0] if sources else None
            base = source.stem if source is not None else _slug(label)
            slug = base
            while slug.casefold() in used:
                # 단사 슬러그가 남기는 유일한 다툼은 대소문자만 다른 라벨이다
                # — 파일시스템이 둘을 한 이름으로 보기 때문이고, 이건 이름
                # 규칙이 아니라 저장소의 제약이다. 순번 대신 지문을 붙여
                # 적어도 몇 번째 시도인지에는 기대지 않는다.
                slug = f"{slug}-{_label_digest(slug)}"
            used.add(slug.casefold())
            paths[label] = ent_dir / f"{slug}.md"
            notes_sources[label] = source
        if source is None:
            continue
        source_notes = existing_notes(source)
        for candidate in quarantined.get(label, []):
            if candidate == source or existing_notes(candidate) == source_notes:
                restored.append(candidate)
    stale.extend(existing.values())
    return EntityPagePlan(
        paths=paths,
        notes_sources=notes_sources,
        stale=tuple(stale),
        restored=tuple(restored),
    )


def demote_stale_entity_pages(plan: EntityPagePlan, wiki: Path) -> None:
    """Move stale generated pages to a non-canonical lane without losing notes."""
    _require_entity_directory(wiki)
    hypotheses = wiki / "hypotheses"
    if plan.stale:
        hypotheses = _ensure_entity_directory(wiki, "hypotheses")
        quarantine = _ensure_entity_directory(hypotheses, "entities")
    else:
        if not _require_entity_directory(hypotheses, missing_ok=True):
            if plan.restored:
                raise OSError(
                    f"wiki entity quarantine disappeared before restore: "
                    f"{hypotheses}"
                )
            return
        quarantine = hypotheses / "entities"
        if not _require_entity_directory(quarantine, missing_ok=True):
            if plan.restored:
                raise OSError(
                    f"wiki entity quarantine disappeared before restore: "
                    f"{quarantine}"
                )
            return
    quarantined: dict[str, list[Path]] = {}
    for path in _entity_pages(quarantine):
        label = _generated_entity(path, _QUARANTINED_ENTITY_TYPE)
        if label is not None:
            quarantined.setdefault(label, []).append(path)
    for source in plan.stale:
        _require_entity_page(source)
        source_text = source.read_text(encoding="utf-8")
        text = source_text.replace(
            _GENERATED_ENTITY_TYPE,
            f"{_QUARANTINED_ENTITY_TYPE}\npromoted: false",
            1,
        )
        label = _generated_entity(source, _GENERATED_ENTITY_TYPE)
        candidates = quarantined.get(label, []) if label is not None else []
        source_notes = _notes_from_text(source_text)
        duplicate = False
        for candidate in candidates:
            _require_entity_page(candidate)
            candidate_text = candidate.read_text(encoding="utf-8")
            if (
                candidate_text == text
                or _notes_from_text(candidate_text) == source_notes
            ):
                duplicate = True
                break
        if duplicate:
            source.unlink()
            continue
        destination = quarantine / source.name
        suffix = 2
        while destination.exists():
            destination = quarantine / f"{source.stem}-{suffix}{source.suffix}"
            suffix += 1
        write_text_atomic(destination, text)
        if label is not None:
            quarantined.setdefault(label, []).append(destination)
        source.unlink()
    for source in plan.restored:
        _require_entity_page(source)
        source.unlink(missing_ok=True)
