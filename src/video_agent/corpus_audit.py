"""Read-only, deterministic readiness aggregation across video workspaces."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import TypedDict

from .access_log import AccessSummary, summarize_access_log
from .checkpoint_store import load_checkpoint_history_with_entries
from .corpus_projection import narrative_head
from .image_index import (
    ImageIndexConsistency,
    image_index_consistency,
    image_index_is_stale,
)
from .narrative_audit import QualifierDropIssue, audit_narrative_qualifiers
from .relation_candidates import RelationCandidate, candidates_from_checkpoints
from .revision import RevisionBindingError, current_revision_bindings
from .search import corpus_root, find_workspaces
from .status import _workspace_status_snapshot_from_entries
from .transcript_segments import normalize_transcript_segments
from .verification_types import VerificationIssue
from .verify_priority import TriggerAlignment, trigger_alignment_from_history
from .wiki_audit import (
    WIKI_INDEX_BYTE_LIMIT as WIKI_INDEX_BYTE_LIMIT,
)
from .wiki_audit import (
    ImprovementCandidates,
    WikiIntegrityAudit,
    WikiLayerAudit,
    audit_wiki_integrity,
    audit_wiki_layer,
    collect_improvements,
)
from .wiki_procedures import (
    ProcedureEvidenceAudit,
    ProcedureMaturity,
    ProcedureStep,
    UnverifiedTokenAudit,
    collect_procedure_steps,
    merge_procedure_steps,
    procedure_evidence_audit,
    procedure_maturity,
    unverified_token_audit,
)
from .workspace import Workspace, load_json


class CorpusWorkspaceAudit(TypedDict):
    """Readiness and evidence totals for one manifest-backed workspace."""

    path: str
    readiness: str
    readiness_legacy: str
    verified_ratio: float
    supported_verified_ratio: float
    terminal_count: int
    supported_count: int
    issue_counts: dict[str, int]
    issues: list[VerificationIssue]
    qualifier_drops: list[QualifierDropIssue]


class ImageConsistencyAudit(TypedDict):
    """촬영된 스틸이 사람이 읽는 인덱스까지 도달했는가.

    게이트가 아니라 보고 축이다 — 낡은 발행은 잘못된 사실이 아니라 아직
    옮겨지지 않은 사실이고, `va index`/`va view` 한 번이면 닫힌다.
    """

    workspace_count: int
    mismatched_count: int
    catalog_total: int
    indexed_total: int
    rows: list[ImageIndexConsistency]


class CorpusAudit(TypedDict):
    """Stable aggregate readiness projection for a discovered workspace corpus."""

    workspace_count: int
    terminal_count: int
    supported_count: int
    issue_counts: dict[str, int]
    qualifier_drop_total: int
    readiness_counts: dict[str, int]
    readiness_legacy_counts: dict[str, int]
    verification_levels_declared: dict[str, int]
    verification_levels_grounded: dict[str, int]
    access: AccessSummary
    # 워크스페이스가 새 증거를 받을 수 있는지. 미결박(legacy)은 읽기 전용이며
    # 이해 루프를 이어갈 수 없다(ADR-0005). 이 분포가 보이지 않으면 코퍼스가
    # 통째로 동결돼도 아무 표면이 말하지 않는다(2026-07-28 실측: 40/40 미결박).
    binding_counts: dict[str, int]
    # 검증 전이가 당시 우선순위 top-3을 겨눴는가 — 시각 검증 과소/과잉
    # 트리거 오판(#49 진입 기준)을 감으로가 아니라 이 축으로 센다.
    trigger_alignment: TriggerAlignment
    # task-* 절차의 재등장·시각 실지지 성숙도 — "많이 쌓였다"의 기계 판정
    # (지식→스킬 트랙 P1, 큐 #52).
    procedure_maturity: list[ProcedureMaturity]
    # 절차 스텝의 근거 종류 분포. 성숙도는 시각 비율 하나로 접어 버리므로
    # "무근거로 실린 단계"가 전사 근거와 구분되지 않는다 — 열거를 그대로 센다.
    procedure_evidence: ProcedureEvidenceAudit
    # exact_tokens가 시각 확정을 암시하는 동안 발화 단독 근거 토큰은 확정과
    # 섞여 읽힌다 — 근거 열거를 이미 쥐고 있으니 그 교집합을 센다.
    procedure_unverified_tokens: UnverifiedTokenAudit
    # 원장이 아는 이미지와 발행된 이미지 인덱스의 3자 대조. 촬영은 원장에만
    # append되고 투영은 따로 렌더되므로, 그 사이 스틸은 조용히 미발행된다.
    image_consistency: ImageConsistencyAudit
    workspaces: list[CorpusWorkspaceAudit]
    relation_candidate_total: int
    relation_candidates: list[RelationCandidate]
    wiki: WikiLayerAudit | None
    wiki_integrity: "WikiIntegrityAudit | None"
    improvements: "ImprovementCandidates | None"


class CorpusAuditError(ValueError):
    """No manifest-backed workspaces were found under the requested roots."""

    def __init__(self, roots: list[str] | None) -> None:
        self.roots = tuple(roots or ["./va-out"])
        super().__init__(str(self))

    def __str__(self) -> str:
        return "no workspaces found (looked in: " + ", ".join(self.roots) + ")"


def _sorted_counts(counts: Counter[str]) -> dict[str, int]:
    """Return a normal dictionary whose insertion order is deterministic."""
    return dict(sorted(counts.items()))


# 행별 상세 목록만 자르는 표시 상한 — 총계(qualifier_drop_total)는 무절단.
_QUALIFIER_DETAIL_CAP = 20
# 관계 후보도 같은 규약 — 총계는 무절단, 목록만 자른다.
_RELATION_CANDIDATE_DETAIL_CAP = 20


# 불일치 행만 자르는 표시 상한 — 총계(mismatched_count)는 무절단.
_IMAGE_CONSISTENCY_DETAIL_CAP = 20


def _image_consistency_audit(
    rows: list[ImageIndexConsistency],
) -> "ImageConsistencyAudit":
    """이미지가 하나도 없는 워크스페이스는 이 축의 모수가 아니다."""
    relevant = [
        row for row in rows if row["catalog"] or row["indexed"]
    ]
    mismatched = [row for row in relevant if image_index_is_stale(row)]
    return {
        "workspace_count": len(relevant),
        "mismatched_count": len(mismatched),
        "catalog_total": sum(row["catalog"] for row in relevant),
        "indexed_total": sum(row["indexed"] for row in relevant),
        "rows": mismatched[:_IMAGE_CONSISTENCY_DETAIL_CAP],
    }


def _binding_state(ws: Workspace) -> str:
    """새 증거를 받을 수 있는 상태인가 — 사용자가 겪는 결과로 이름 붙인다.

    결박이 아직 없다고 해서 전부 동결은 아니다. `revision_draft`는 첫 기입
    때 결박을 발행하도록 `bind_record_to_workspace`가 허용하는 상태다
    (revision.py). 이것까지 legacy로 세면 "재-ingest하라"는 잘못된 지시가
    막 만든 워크스페이스에 붙는다.
    """
    try:
        if current_revision_bindings(ws) is not None:
            return "bound"
    except RevisionBindingError:
        return "incomplete"
    except (OSError, ValueError):
        return "unreadable"
    try:
        draft = ws.manifest.get("revision_draft") is True
    except (OSError, ValueError):
        return "unreadable"
    return "draft" if draft else "legacy"


def audit_corpus(
    roots: list[str] | None = None,
    *,
    workspace_paths: Sequence[Path] | None = None,
    projection_root: Path | None = None,
) -> CorpusAudit:
    """Aggregate status snapshots without writing any discovered workspace."""
    discovered = (
        list(workspace_paths)
        if workspace_paths is not None
        else find_workspaces(roots)
    )
    paths = sorted(
        {path.resolve() for path in discovered},
        key=lambda path: str(path),
    )
    if not paths:
        raise CorpusAuditError(roots)
    root = (
        Path(projection_root).resolve()
        if projection_root is not None
        else corpus_root(roots)
    )

    terminal_count = 0
    supported_count = 0
    issue_counts: Counter[str] = Counter()
    readiness_counts: Counter[str] = Counter()
    readiness_legacy_counts: Counter[str] = Counter()
    binding_counts: Counter[str] = Counter()
    verification_levels_declared: Counter[str] = Counter()
    verification_levels_grounded: Counter[str] = Counter()
    narrative_missing: list[str] = []
    qualifier_drop_total = 0
    workspaces: list[CorpusWorkspaceAudit] = []
    candidates: list[RelationCandidate] = []
    verify_transitions = 0
    aligned_top3 = 0
    procedure_steps: dict[str, list[ProcedureStep]] = {}
    image_rows: list[ImageIndexConsistency] = []
    for path in paths:
        ws = Workspace.load(path)
        # 원장(이력+스냅샷)도 전사도 한 판독에서 파생한다 — 사이에 다른
        # 기입자가 파일을 교체하면 한 감사가 서로 다른 상태의 지표를 섞는다.
        history, entries = load_checkpoint_history_with_entries(ws)
        raw_transcript = load_json(ws.transcript_path)
        snapshot = _workspace_status_snapshot_from_entries(
            ws, entries, transcript_value=raw_transcript
        )
        alignment = trigger_alignment_from_history(
            ws, history,
            segments=normalize_transcript_segments(raw_transcript),
        )
        verify_transitions += alignment["verify_transitions"]
        aligned_top3 += alignment["aligned_top3"]
        status = snapshot.status
        qualifier_drops = audit_narrative_qualifiers(
            ws, snapshot.terminal_checkpoints
        )
        qualifier_drop_total += len(qualifier_drops)
        candidates.extend(
            candidates_from_checkpoints(path.name, snapshot.checkpoints))
        if (status["readiness"]["status"] == "converged"
                and narrative_head(ws) is None):
            narrative_missing.append(path.name)
        binding_counts.update([_binding_state(ws)])
        try:
            procedure_workspace_id = path.relative_to(root).as_posix()
        except ValueError:
            procedure_workspace_id = path.name
        image_rows.append(
            image_index_consistency(ws, workspace_id=procedure_workspace_id)
        )
        merge_procedure_steps(
            procedure_steps,
            collect_procedure_steps(
                procedure_workspace_id,
                snapshot.supported_checkpoints,
                {
                    levels.checkpoint_id: levels.grounded
                    for levels in snapshot.verification.checkpoint_levels
                },
            ),
        )
        for levels in snapshot.verification.checkpoint_levels:
            verification_levels_declared.update([levels.declared])
            verification_levels_grounded.update([levels.grounded])
        evidence = status["verification_audit"]
        readiness = status["readiness"]["status"]
        readiness_legacy = status["readiness_legacy"]["status"]
        local_issue_counts = Counter(issue["code"] for issue in evidence["issues"])
        terminal_count += evidence["terminal_count"]
        supported_count += evidence["supported_count"]
        issue_counts.update(local_issue_counts)
        readiness_counts.update([readiness])
        readiness_legacy_counts.update([readiness_legacy])
        workspaces.append(
            {
                "path": str(path),
                "readiness": readiness,
                "readiness_legacy": readiness_legacy,
                "verified_ratio": status["verified_ratio"],
                "supported_verified_ratio": evidence["supported_verified_ratio"],
                "terminal_count": evidence["terminal_count"],
                "supported_count": evidence["supported_count"],
                "issue_counts": _sorted_counts(local_issue_counts),
                "issues": evidence["issues"],
                "qualifier_drops": qualifier_drops[:_QUALIFIER_DETAIL_CAP],
            }
        )
    return {
        "workspace_count": len(workspaces),
        "terminal_count": terminal_count,
        "supported_count": supported_count,
        "issue_counts": _sorted_counts(issue_counts),
        "qualifier_drop_total": qualifier_drop_total,
        "readiness_counts": _sorted_counts(readiness_counts),
        "readiness_legacy_counts": _sorted_counts(readiness_legacy_counts),
        "verification_levels_declared": _sorted_counts(verification_levels_declared),
        "verification_levels_grounded": _sorted_counts(verification_levels_grounded),
        # 근거 품질 옆에 소비량을 둔다 — 아무도 읽지 않는 코퍼스는
        # 근거가 아무리 좋아도 지식으로 기능하지 않는다.
        "access": summarize_access_log(root),
        "binding_counts": _sorted_counts(binding_counts),
        "trigger_alignment": {
            "verify_transitions": verify_transitions,
            "aligned_top3": aligned_top3,
            "alignment_ratio": (
                round(aligned_top3 / verify_transitions, 4)
                if verify_transitions
                else None
            ),
        },
        "procedure_maturity": procedure_maturity(procedure_steps),
        "procedure_evidence": procedure_evidence_audit(procedure_steps),
        "procedure_unverified_tokens": unverified_token_audit(procedure_steps),
        "image_consistency": _image_consistency_audit(image_rows),
        "workspaces": workspaces,
        "relation_candidate_total": len(candidates),
        "relation_candidates": candidates[:_RELATION_CANDIDATE_DETAIL_CAP],
        "wiki": audit_wiki_layer(root),
        "wiki_integrity": audit_wiki_integrity(root),
        "improvements": collect_improvements(
            root, narrative_missing),
    }
