"""Cross-workspace lexical search over accumulated understanding artifacts.

The kilobyte-scale text artifacts (transcript segments, checkpoints, OCR
pseudo-transcript) of every workspace under a va-out root are indexed into a
persistent SQLite FTS5 index at `<corpus>/.tca-search-cache.db` — synced
incrementally per workspace fingerprint, so a query touches only the inverted
index instead of re-parsing and re-indexing the whole corpus. Korean text is
NFC-normalized on both sides (macOS paths arrive NFD). Two retrievers per
source family, fused by reciprocal rank: word FTS (unicode61, prefix-matched
so particle-suffixed forms still hit — "치킨" matches "치킨을") and trigram
FTS (substring recall for inflected or unsegmented Korean — "클리핑" inside
"미러클리핑머지"). Plain substring scan remains the fallback when both find
nothing (e.g. sub-3-char inner tokens like "디어" in "드디어"). Any index
failure falls back to direct collection and in-memory ranking.
"""

from __future__ import annotations

import json
import sqlite3
import unicodedata
from collections.abc import Sequence
from pathlib import Path

from .checkpoints import load_checkpoints
from .transcript_segments import load_transcript_segments
from .workspace import Workspace
from .workspace_discovery import corpus_root as corpus_root
from .workspace_discovery import find_workspaces


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def _to_text(value) -> str:
    """Flatten str | dict | list checkpoint fields to searchable text.

    Real workspaces store speakers/entities either as plain strings or as
    dicts like {"label": "멤버A", "basis": "명패"} — index the string values
    of both shapes.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_to_text(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return " ".join(_to_text(v) for v in value)
    return ""


# ASR 세그먼트는 짧다(실측 코퍼스 평균 20자). 구절이 경계로 갈리면 "A B"
# 질의가 어느 세그먼트에도 온전히 들어있지 않아 통째로 놓친다. 인접 세그먼트를
# 묶은 윈도우를 보조 색인으로 함께 넣고, 히트는 다시 실제 세그먼트로 좁힌다.
_WINDOW_SEGMENTS = 3
# 무발화로 멀리 떨어진 세그먼트를 한 윈도우로 묶으면 없는 근접성을 만들어낸다.
_WINDOW_MAX_GAP_S = 5.0

# 검색 계열 — 문서 수가 아니라 계열 내 순위로 겨루게 한다. 전사는 체크포인트
# 보다 수십 배 많아 단일 랭킹에서는 이해가 원시 전사에 묻힌다.
_SOURCE_FAMILY = {
    "checkpoint": "understanding",
    "transcript": "transcript",
    "transcript-window": "transcript",
    "ocr": "screen",
}
_RRF_K = 60  # reciprocal rank fusion 표준 상수
# 영속 인덱스의 계열 순회 순서 — _SOURCE_FAMILY 치역 + 미지 소스의 "other".
_INDEX_FAMILIES = ("other", "screen", "transcript", "understanding")
# RRF 동점 타이브레이크 — 판정된 이해 > 화면 사실 > 원시 전사. 계열을
# 분리해 랭킹한 이유(전사 물량에 이해가 묻히지 않게)를 동점에서도 지킨다.
_FAMILY_PRIORITY = {"understanding": 0, "screen": 1, "transcript": 2}


def _family(source: str) -> str:
    return _SOURCE_FAMILY.get(source.split(":")[0], "other")


def _transcript_windows(seg_docs: list[dict]) -> list[dict]:
    """인접 전사 세그먼트 윈도우 — 경계로 갈린 구절 회상용 보조 문서."""
    windows: list[dict] = []
    total = len(seg_docs)
    for i in range(total):
        members = [seg_docs[i]]
        for j in range(i + 1, min(i + _WINDOW_SEGMENTS, total)):
            if seg_docs[j]["start"] - members[-1]["end"] > _WINDOW_MAX_GAP_S:
                break
            members.append(seg_docs[j])
        if len(members) < 2:
            continue
        windows.append({
            "ws": members[0]["ws"],
            "source": "transcript-window",
            "start": members[0]["start"],
            "end": members[-1]["end"],
            "text": " ".join(m["text"] for m in members),
            "members": members,
        })
    return windows


def collect_docs(ws: Workspace, *, windows: bool = True) -> list[dict]:
    """One doc per searchable unit: transcript seg / checkpoint / OCR span.

    본문은 여기서 한 번 NFC로 맞춰 둔다(macOS 경로·파일은 NFD로 들어온다) —
    이후 색인·점수 계산은 정규화를 다시 하지 않는다.

    windows=False면 전사 윈도우를 만들지 않는다. 윈도우는 공백으로 이어붙인
    보조 문서라 토큰도 부분 문자열도 멤버 경계를 넘지 못한다 — 단일어 질의에서
    윈도우가 맞으면 그 멤버 세그먼트도 반드시 맞으므로 회상 이득이 없고
    색인 비용만 는다.
    """
    docs: list[dict] = []
    name = ws.root.name
    segs = load_transcript_segments(ws.transcript_path)
    seg_docs = [{"ws": name, "source": "transcript",
                 "start": s["start"], "end": s["end"],
                 "text": _nfc(s["text"].strip())}
                for s in segs if s.get("text", "").strip()]
    docs.extend(seg_docs)
    if windows:
        docs.extend(_transcript_windows(seg_docs))
    for c in load_checkpoints(ws):
        # exact_tokens = 화면에서 시각 복원한 UI 토큰 — 산문(hypothesis)은
        # 흔히 한국어 표기("정면 직교 뷰")만 남겨 영문 토큰("Front
        # Orthographic") 질의를 놓친다. 실측 16cp에서 산문 부재 확인
        # (AnyGold 벤치 exact-token 0.429), 검색 가치가 가장 높은
        # 문자열이므로 함께 색인한다.
        parts = [_to_text(c.get("hypothesis")),
                 _to_text(c.get("situation")),
                 _to_text(c.get("note")),
                 _to_text(c.get("exact_tokens")),
                 _to_text(c.get("speakers")),
                 _to_text(c.get("entities"))]
        text = _nfc(" ".join(p for p in parts if p).strip())
        if text:
            span = c.get("span")
            if (
                not isinstance(span, (list, tuple))
                or len(span) != 2
                or any(
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    for value in span
                )
            ):
                continue
            start, end = span
            docs.append({"ws": name, "source": f"checkpoint:{c['id']}",
                         "status": c["status"],
                         "start": start, "end": end,
                         "text": text})
    ocr_path = ws.root / "ocr_transcript.json"
    if ocr_path.is_file():
        try:
            for o in json.loads(ocr_path.read_text(encoding="utf-8")):
                if o.get("text", "").strip():
                    docs.append({"ws": name, "source": "ocr",
                                 "start": o["start"], "end": o["end"],
                                 "text": _nfc(o["text"].strip())})
        except (json.JSONDecodeError, KeyError, TypeError):
            pass
    return docs


def _fts_query(query: str) -> str:
    """Each term as a prefix phrase: 치킨 순간 -> "치킨"* "순간"*."""
    terms = [t for t in _nfc(query).split() if t]
    return " ".join('"{}"*'.format(t.replace('"', '""')) for t in terms)


def _tri_query(terms: list[str]) -> str:
    """3자 이상 항만 트라이그램 구절로 — 짧은 항은 후검증으로 넘긴다.

    트라이그램 토크나이저는 3자 미만 항의 토큰을 아예 못 만든다. 짧은 항을
    질의에 남기면 매치가 항상 실패하므로 긴 항만 MATCH(AND)하고, 짧은 항의
    AND 의미는 `_short_term_filter`가 부분 문자열로 마저 지킨다.
    """
    tri = [t for t in terms if len(t) >= 3]
    return " ".join('"{}"'.format(t.replace('"', '""')) for t in tri)


def _short_term_filter(hits: list[dict], terms: list[str]) -> list[dict]:
    """트라이그램 질의에서 빠진 3자 미만 항의 AND 의미를 되살린다."""
    short = [t for t in terms if len(t) < 3]
    if not short:
        return hits
    return [h for h in hits if all(t in h["text"] for t in short)]


def _resolve_windows(hits: list[dict], terms: list[str]) -> list[dict]:
    """윈도우 히트를 말이 실제로 나온 세그먼트로 좁히고 중복 구간을 접는다.

    윈도우는 회상을 넓히려고 둔 보조 색인이다. 결과까지 윈도우 구간으로
    돌려주면 인용이 뭉뚱그려지므로, 질의어를 가장 많이 담은 멤버 세그먼트의
    타임스탬프로 되돌린다. 컷은 여기서 하지 않는다 — 융합이 top을 정한다.
    """
    seen: set[tuple] = set()
    out: list[dict] = []
    for hit in hits:              # 점수 내림차순 — 먼저 온 쪽이 최고 점수다
        if hit.get("members"):
            best = max(hit["members"],
                       key=lambda m: sum(m["text"].count(t) for t in terms))
            hit = {**hit, "source": "transcript", "start": best["start"],
                   "end": best["end"], "text": best["text"]}
            del hit["members"]
        key = (hit["ws"], hit["source"], hit["start"], hit["end"])
        if key not in seen:
            seen.add(key)
            out.append(hit)
    return out


def _mem_fts_rank(
    docs: list[dict], match: str, limit: int, tokenize: str
) -> list[dict]:
    """BM25-ranked FTS5 search over one family of in-memory documents."""
    con = sqlite3.connect(":memory:")
    hits: list[dict] = []
    # 테이블 생성까지 가드 안에 둔다 — trigram 토크나이저가 없는 SQLite
    # 빌드에서 인덱스 폴백 뒤 인메모리 경로마저 죽으면 안 된다(워드 검색과
    # 부분 문자열 폴백은 여전히 유효하다).
    try:
        con.execute(
            f"CREATE VIRTUAL TABLE d USING fts5(text, tokenize='{tokenize}')")
        con.executemany("INSERT INTO d(rowid, text) VALUES (?, ?)",
                        [(i, doc["text"]) for i, doc in enumerate(docs)])
        rows = con.execute(
            "SELECT rowid, rank FROM d WHERE d MATCH ? "
            "ORDER BY rank LIMIT ?", (match, limit)).fetchall()
        hits = [{**docs[r], "score": round(-rank, 2)} for r, rank in rows]
    except sqlite3.OperationalError:
        hits = []
    con.close()
    return hits


def _substring_rank(
    docs: list[dict], terms: list[str], limit: int
) -> list[dict]:
    """Fallback for sub-token matches the tokenizers split away ("디어")."""
    hits: list[dict] = []
    for doc in docs:
        text = doc["text"]
        count = sum(text.count(t) for t in terms)
        if count and all(t in text for t in terms):
            hits.append({**doc, "score": float(count)})
    hits.sort(key=lambda h: -h["score"])
    return hits[:limit]


def _coverage_needed(term_count: int) -> int:
    """과반 커버리지 문턱 — 2항 이하는 전항(=엄격 AND과 동일)이 된다."""
    return term_count // 2 + 1


def _coverage_rank(
    docs: list[dict], terms: list[str], limit: int
) -> list[dict]:
    """주제형 다항 질의의 과반 일치 랭킹 — 엄격 AND이 전무할 때만 쓴다.

    "커브 헤어 갈래 분리"처럼 항이 체크포인트 여러 장에 분산되면 어떤
    문서도 전항을 담지 못해 AND이 0건이 된다(회상 벤치 q04·q05 실측).
    과반(> n/2)을 담은 문서를 항 수·출현 수 순으로 돌려주되, 히트에
    coverage(일치 항 수)를 실어 부분 일치임을 드러낸다.
    """
    # 중복 항은 커버리지를 부풀린다("foo foo missing"이 2/3 통과) — 고유
    # 항 기준으로 문턱·일치를 센다. 워드 FTS와 같은 대소문자 무구분을
    # 지키기 위해 양쪽을 소문자로 접는다.
    unique = list(dict.fromkeys(t.lower() for t in terms))
    need = _coverage_needed(len(unique))
    scored: list[tuple[int, int, dict]] = []
    for doc in docs:
        text = doc["text"].lower()
        matched = sum(1 for t in unique if t in text)
        if matched < need:
            continue
        occurrences = sum(text.count(t) for t in unique)
        scored.append((matched, occurrences, doc))
    scored.sort(key=lambda item: (-item[0], -item[1]))
    return [{**doc, "coverage": matched, "score": float(matched)}
            for matched, _, doc in scored[:limit]]


def _fuse_retrievers(
    word: list[dict], tri: list[dict], pool: int
) -> list[dict]:
    """계열 내 워드·트라이그램 랭킹 융합 — 한쪽뿐이면 그대로 쓴다."""
    if word and tri:
        return _rrf_fuse([word, tri], pool)
    return word or tri


def _rrf_fuse(ranked_lists: list[list[dict]], top: int) -> list[dict]:
    """Reciprocal rank fusion — 계열의 문서 수가 아니라 계열 내 순위로 겨룬다."""
    fused: dict[tuple, dict] = {}
    for ranked in ranked_lists:
        for rank, hit in enumerate(ranked, start=1):
            key = (hit["ws"], hit["source"], hit["start"], hit["end"])
            contribution = 1.0 / (_RRF_K + rank)
            if key in fused:
                fused[key]["score"] += contribution
            else:
                fused[key] = {**hit, "score": contribution}
    out = sorted(
        fused.values(),
        key=lambda h: (-h["score"],
                       _FAMILY_PRIORITY.get(_family(h["source"]), 9),
                       h["ws"], h["source"]))
    for hit in out:
        hit["score"] = round(hit["score"], 4)
    return out[:top]


def search_docs(docs: list[dict], query: str, top: int = 10) -> list[dict]:
    """계열별 하이브리드 BM25 랭킹을 RRF로 융합한다(부분 문자열 폴백).

    전사는 체크포인트보다 문서가 수십 배 많아, 단일 랭킹에서는 같은 말이
    전사에 여러 번 나오기만 해도 상위를 덮고 판정된 이해가 밀려난다. 전사·
    이해·화면을 따로 랭킹해 융합하면 각 계열의 최상위가 자리를 얻는다.

    계열 안에서는 워드(접두)와 트라이그램(부분 문자열) 두 검색기를 다시
    융합한다 — 조사·활용으로 어형이 바뀐 한국어("클리핑"이 "미러클리핑머지"
    안에 있는 경우)를 워드 검색 단독보다 넓게 회상한다.

    반환 score는 BM25 값이 아니라 RRF 점수다 — 순위 신호이며 절대 크기에
    의미가 없다.
    """
    terms = [t for t in _nfc(query).split() if t]
    if not terms:
        return []
    families: dict[str, list[dict]] = {}
    for doc in docs:
        families.setdefault(_family(doc["source"]), []).append(doc)
    # 세그먼트와 그것을 품은 윈도우가 함께 맞으므로, 좁히고 접은 뒤 top을
    # 채우려면 넉넉히 뽑아야 한다.
    pool = top * 4
    fq = _fts_query(query)
    tq = _tri_query(terms)
    # 폴백은 계열마다 판정한다. 전역으로 보면 한 계열이 FTS로 맞는 순간
    # 나머지 계열의 사이 토큰 적중이 통째로 사라진다 — "디어"가 이해에는
    # 단독 토큰으로 있고 전사에는 "드디어" 안에만 있을 때 전사를 놓쳤다.
    # 전사 계열 전체 스캔은 실측 0.7ms(4,008 doc)로 회상과 바꿀 값이 아니다.
    ranked: list[list[dict]] = []
    for family_docs in families.values():
        word = _mem_fts_rank(family_docs, fq, pool, "unicode61") if fq else []
        tri = []
        if tq:
            tri = _short_term_filter(
                _mem_fts_rank(family_docs, tq, pool, "trigram"), terms)
        hits = _fuse_retrievers(word, tri, pool)
        if not hits:
            hits = _substring_rank(family_docs, terms, pool)
        if hits:
            ranked.append(_resolve_windows(hits, terms))
    if not ranked and len(terms) >= 3:
        # 전 계열 0건 + 3항 이상 — 과반 커버리지 폴백(정확 질의 희석 방지:
        # 엄격 AND이 한 건이라도 있으면 절대 발동하지 않는다).
        for family_docs in families.values():
            hits = _coverage_rank(family_docs, terms, pool)
            if hits:
                ranked.append(_resolve_windows(hits, terms))
    return _rrf_fuse(ranked, top)


# 영속 검색 인덱스 — 매 질의가 코퍼스 전 워크스페이스의 transcript/checkpoint/
# OCR JSON을 다시 파싱하고 인메모리 FTS를 재구축하던 것을(둘 다 코퍼스 크기에
# 선형, 57ws/1.9만 doc 실측 워밍 114ms), 지문 기반 워크스페이스 단위 증분
# 동기화가 유지하는 온디스크 FTS5 역색인으로 바꾼다. 인덱스 실패는 전부 직접
# 수집+인메모리 랭킹으로 조용히 폴백한다. 문서 형태를 바꾸면 솔트가 소스
# digest에서 자동으로 따라온다.
_SEARCH_CACHE_FILENAME = ".tca-search-cache.db"

_INDEX_DDL = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS ws_meta(
  ws_key TEXT PRIMARY KEY, fp TEXT, doc_count INTEGER);
CREATE TABLE IF NOT EXISTS docs(
  id INTEGER PRIMARY KEY, ws_key TEXT NOT NULL, ws TEXT NOT NULL,
  source TEXT NOT NULL, status TEXT, start REAL, end REAL,
  text TEXT NOT NULL, family TEXT NOT NULL,
  is_window INTEGER NOT NULL DEFAULT 0, members TEXT);
CREATE INDEX IF NOT EXISTS docs_ws_key ON docs(ws_key);
CREATE VIRTUAL TABLE IF NOT EXISTS fts_word USING fts5(
  text, content='docs', content_rowid='id', tokenize='unicode61');
CREATE VIRTUAL TABLE IF NOT EXISTS fts_tri USING fts5(
  text, content='docs', content_rowid='id', tokenize='trigram');
CREATE TRIGGER IF NOT EXISTS docs_ai AFTER INSERT ON docs BEGIN
  INSERT INTO fts_word(rowid, text) VALUES (new.id, new.text);
  INSERT INTO fts_tri(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS docs_ad AFTER DELETE ON docs BEGIN
  INSERT INTO fts_word(fts_word, rowid, text)
    VALUES ('delete', old.id, old.text);
  INSERT INTO fts_tri(fts_tri, rowid, text)
    VALUES ('delete', old.id, old.text);
END;
"""


def _search_salt() -> str:
    # 문서 추출·색인 로직(이 모듈)이 바뀌면 인덱스도 무효 — 수동 버전 대신
    # 소스 digest에서 유도한다(투영 캐시와 같은 계약).
    from .projection_cache import renderer_salt

    return renderer_salt("search")


def _cache_key(ws_dir: Path, corpus: Path) -> str:
    """그룹이 달라도 leaf 이름이 같을 수 있다 — 코퍼스 상대경로가 키다."""
    try:
        return ws_dir.relative_to(corpus).as_posix()
    except ValueError:
        return ws_dir.name


_INDEX_TABLES = ("docs", "ws_meta", "fts_word", "fts_tri")


def _open_index(corpus: Path) -> sqlite3.Connection:
    con = sqlite3.connect(corpus / _SEARCH_CACHE_FILENAME, timeout=5.0)
    try:
        con.execute("PRAGMA busy_timeout = 5000")
        con.execute("DROP TABLE IF EXISTS ws_docs")  # 구 문서-JSON 캐시
        # 솔트 검사가 DDL보다 먼저다: 솔트가 다르면 스키마도 다를 수
        # 있다(소스 digest가 곧 스키마 버전). 낡은 테이블 위에 새 DDL을
        # 먼저 돌리면 없는 컬럼을 참조하다 죽는다 — 테이블째 재생성으로
        # 이행한다.
        con.execute(
            "CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,"
            " value TEXT)")
        salt = _search_salt()
        row = con.execute(
            "SELECT value FROM meta WHERE key='salt'").fetchone()
        if row is None or row[0] != salt:
            for table in _INDEX_TABLES:
                con.execute(f"DROP TABLE IF EXISTS {table}")
            con.execute(
                "INSERT OR REPLACE INTO meta(key, value)"
                " VALUES ('salt', ?)", (salt,))
        con.executescript(_INDEX_DDL)
        con.commit()
    except sqlite3.Error:
        con.close()
        raise
    return con


def _doc_row(key: str, doc: dict) -> tuple:
    # ws는 문서가 밖으로 들고 나가는 공개 워크스페이스명(leaf), ws_key는
    # 그룹이 달라도 안 겹치는 캐시 정체성 — 둘을 섞으면 그룹 코퍼스에서
    # 인덱스 경로와 직접 수집 경로의 히트 ws가 달라진다.
    members = doc.get("members")
    return (key, doc["ws"], doc["source"], doc.get("status"),
            doc.get("start"), doc.get("end"), doc["text"],
            _family(doc["source"]),
            1 if doc["source"] == "transcript-window" else 0,
            json.dumps(members, ensure_ascii=False) if members else None)


def _sync_index(
    con: sqlite3.Connection, paths: Sequence[Path], corpus: Path
) -> None:
    """지문이 바뀐 워크스페이스만 다시 색인하고 죽은 키를 친다.

    지문 일치만으로는 바깥 훼손(행 삭제)을 못 본다 — 행 수를 함께 대조해
    어긋나면 그 워크스페이스를 재색인한다(구 JSON 캐시의 행 단위 손상
    폴백과 같은 자가치유 계약).
    """
    salt = _search_salt()
    from .projection_cache import workspace_fingerprint

    live: set[str] = set()
    for ws_dir in paths:
        key = _cache_key(ws_dir, corpus)
        live.add(key)
        fingerprint = workspace_fingerprint(ws_dir, salt=salt)
        row = con.execute(
            "SELECT fp, doc_count FROM ws_meta WHERE ws_key = ?", (key,)
        ).fetchone()
        if row and row[0] == fingerprint:
            (count,) = con.execute(
                "SELECT count(*) FROM docs WHERE ws_key = ?",
                (key,)).fetchone()
            if count == row[1]:
                continue
        try:
            ws_docs = collect_docs(Workspace.load(ws_dir), windows=True)
        except FileNotFoundError:
            con.execute("DELETE FROM docs WHERE ws_key = ?", (key,))
            con.execute("DELETE FROM ws_meta WHERE ws_key = ?", (key,))
            live.discard(key)
            continue
        con.execute("DELETE FROM docs WHERE ws_key = ?", (key,))
        con.executemany(
            "INSERT INTO docs(ws_key, ws, source, status, start, end,"
            " text, family, is_window, members)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            [_doc_row(key, d) for d in ws_docs])
        con.execute(
            "INSERT OR REPLACE INTO ws_meta(ws_key, fp, doc_count)"
            " VALUES (?, ?, ?)",
            (key, fingerprint, len(ws_docs)))
    for (dead,) in con.execute("SELECT ws_key FROM ws_meta").fetchall():
        if dead not in live:
            con.execute("DELETE FROM docs WHERE ws_key = ?", (dead,))
            con.execute("DELETE FROM ws_meta WHERE ws_key = ?", (dead,))
    con.commit()


def _hit_from_row(row: tuple) -> dict:
    ws, source, status, start, end, text, members, rank = row
    hit = {"ws": ws, "source": source, "start": start, "end": end,
           "text": text, "score": round(-rank, 2)}
    if status is not None:
        hit["status"] = status
    if members:
        hit["members"] = json.loads(members)
    return hit


def _index_fts_rank(
    con: sqlite3.Connection, table: str, family: str,
    match: str, pool: int, windows: bool,
) -> list[dict]:
    win = "" if windows else " AND d.is_window = 0"
    try:
        rows = con.execute(
            f"SELECT d.ws, d.source, d.status, d.start, d.end, d.text,"
            f" d.members, {table}.rank"
            f" FROM {table} JOIN docs d ON d.id = {table}.rowid"
            f" WHERE {table} MATCH ? AND d.family = ?{win}"
            # 동점은 재색인해도 안 바뀌는 키로 가른다 — id는 재삽입 때마다
            # 뒤로 밀려 같은 코퍼스에서 순서가 흔들린다.
            f" ORDER BY {table}.rank, d.ws, d.source, d.start LIMIT ?",
            (match, family, pool)).fetchall()
    except sqlite3.OperationalError:
        return []
    return [_hit_from_row(r) for r in rows]


def _index_coverage_rank(
    con: sqlite3.Connection, family: str,
    terms: list[str], pool: int, windows: bool,
) -> list[dict]:
    """`_coverage_rank`의 인덱스 쪽 등가물 — 전 계열 0건일 때만 스캔한다.

    고유 항·소문자 접기 계약은 인메모리 쪽과 동일(SQLite lower()는 ASCII
    한정이지만 한국어는 대소문자가 없어 등가다).
    """
    unique = list(dict.fromkeys(t.lower() for t in terms))
    win = "" if windows else " AND is_window = 0"
    cov = " + ".join("(instr(lower(text), ?) > 0)" for _ in unique)
    rows = con.execute(
        "SELECT ws, source, status, start, end, text, members, cov"
        f" FROM (SELECT ws, source, status, start, end, text, members,"
        f" ({cov}) AS cov FROM docs WHERE family = ?{win})"
        " WHERE cov >= ? ORDER BY cov DESC, ws, source, start",
        (*unique, family, _coverage_needed(len(unique)))).fetchall()
    scored: list[tuple[int, int, dict]] = []
    for r in rows:
        hit = _hit_from_row(r[:7] + (0,))
        lowered = hit["text"].lower()
        occurrences = sum(lowered.count(t) for t in unique)
        hit["coverage"] = r[7]
        hit["score"] = float(r[7])
        scored.append((r[7], occurrences, hit))
    scored.sort(key=lambda item: (-item[0], -item[1]))
    return [hit for _, _, hit in scored[:pool]]


def _index_substring_rank(
    con: sqlite3.Connection, family: str,
    terms: list[str], pool: int, windows: bool,
) -> list[dict]:
    win = "" if windows else " AND is_window = 0"
    clause = " AND ".join("instr(text, ?) > 0" for _ in terms)
    rows = con.execute(
        "SELECT ws, source, status, start, end, text, members, 0"
        f" FROM docs WHERE family = ? AND {clause}{win}"
        " ORDER BY ws, source, start",
        (family, *terms)).fetchall()
    hits = []
    for r in rows:
        hit = _hit_from_row(r)
        hit["score"] = float(sum(hit["text"].count(t) for t in terms))
        hits.append(hit)
    hits.sort(key=lambda h: -h["score"])
    return hits[:pool]


def _index_search(
    con: sqlite3.Connection, query: str, top: int, windows: bool
) -> list[dict]:
    terms = [t for t in _nfc(query).split() if t]
    if not terms:
        return []
    pool = top * 4
    fq = _fts_query(query)
    tq = _tri_query(terms)
    ranked: list[list[dict]] = []
    # 계열은 고정 상수 집합이다 — DISTINCT 스캔(무인덱스, 코퍼스 선형) 없이
    # 정해진 순서로 순회한다. 문서가 없는 계열은 두 검색기와 폴백이 모두
    # 비어 자연히 건너뛴다. 재색인이 순회 순서를 흔들지 못한다.
    for family in _INDEX_FAMILIES:
        word = (_index_fts_rank(con, "fts_word", family, fq, pool, windows)
                if fq else [])
        tri = []
        if tq:
            tri = _short_term_filter(
                _index_fts_rank(con, "fts_tri", family, tq, pool, windows),
                terms)
        hits = _fuse_retrievers(word, tri, pool)
        if not hits:
            hits = _index_substring_rank(con, family, terms, pool, windows)
        if hits:
            ranked.append(_resolve_windows(hits, terms))
    if not ranked and len(terms) >= 3:
        # 전 계열 0건 + 3항 이상 — 과반 커버리지 폴백(인메모리 경로와 동일
        # 문턱: 엄격 AND이 한 건이라도 있으면 발동하지 않는다).
        for family in _INDEX_FAMILIES:
            hits = _index_coverage_rank(con, family, terms, pool, windows)
            if hits:
                ranked.append(_resolve_windows(hits, terms))
    return _rrf_fuse(ranked, top)


def _no_workspaces_error(roots: list[str] | None) -> ValueError:
    return ValueError(
        "no searchable workspaces found (looked in: "
        + ", ".join(roots or ["./va-out"]) + ")")


def search_workspaces(
    query: str,
    roots: list[str] | None = None,
    top: int = 10,
    *,
    workspace_paths: Sequence[Path] | None = None,
    projection_root: Path | None = None,
) -> list[dict]:
    # 단일어 질의는 윈도우에서 얻을 회상이 없다(멤버 경계를 넘는 매칭이
    # 불가능하다) — 색인에는 superset으로 들어 있고 질의에서 걸러낸다.
    windows = len(_nfc(query).split()) >= 2
    if workspace_paths is not None:
        # 리스 러너의 완전 스냅샷은 projection_root를 함께 준다 — 그때만
        # 인덱스를 쓴다. 루트 없는 임의 부분집합(라이브러리 호출)은 인덱스를
        # 우회한다: 부분집합 기준으로 가지치기하면 나머지 색인이 지워진다.
        paths = list(workspace_paths)
        corpus: Path | None = (
            Path(projection_root).resolve()
            if projection_root is not None
            else None
        )
    else:
        paths = find_workspaces(roots)
        try:
            corpus = corpus_root(roots) if paths else None
        except ValueError:
            corpus = None
    if corpus is not None:
        hits: list[dict] | None = None
        try:
            con = _open_index(corpus)
            try:
                _sync_index(con, paths, corpus)
                if con.execute("SELECT 1 FROM docs LIMIT 1").fetchone() \
                        is None:
                    raise _no_workspaces_error(roots)
                hits = _index_search(con, query, top, windows)
            finally:
                con.close()
        except sqlite3.Error:
            hits = None  # 손상·잠김 인덱스 — 직접 수집으로 조용히 폴백
        if hits is not None:
            return hits
    docs: list[dict] = []
    for ws_dir in paths:
        try:
            docs.extend(collect_docs(Workspace.load(ws_dir), windows=windows))
        except FileNotFoundError:
            continue
    if not docs:
        raise _no_workspaces_error(roots)
    return search_docs(docs, query, top=top)
