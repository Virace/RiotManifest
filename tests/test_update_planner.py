"""diff 报告驱动的更新计划器单测."""

from pathlib import Path

from riotmanifest.diff.manifest_diff import (
    ManifestDiffEntry,
    ManifestDiffReport,
    ManifestDiffSummary,
    ManifestMovedEntry,
)
from riotmanifest.manifest import PatcherFile
from riotmanifest.update.planner import FileAction, build_update_plan


def _make_file(name: str, *, link: str = "") -> PatcherFile:
    return PatcherFile(
        name=name,
        size=4,
        link=link,
        flags=None,
        chunks=[],
        manifest=None,
        chunk_hash_types={},
    )


def _entry(path: str, status: str) -> ManifestDiffEntry:
    return ManifestDiffEntry(
        path=path,
        status=status,  # type: ignore[arg-type]
        old_size=None,
        new_size=None,
        old_flags=None,
        new_flags=None,
        old_link=None,
        new_link=None,
        old_chunk_digest=None,
        new_chunk_digest=None,
        changed_fields=(),
    )


def _report(
    *,
    added: tuple[str, ...] = (),
    removed: tuple[str, ...] = (),
    changed: tuple[str, ...] = (),
    unchanged: tuple[str, ...] = (),
    moved: tuple[tuple[str, str], ...] = (),
) -> ManifestDiffReport:
    summary = ManifestDiffSummary(
        total_old=0,
        total_new=0,
        total_common=0,
        added_count=len(added),
        removed_count=len(removed),
        changed_count=len(changed),
        unchanged_count=len(unchanged),
        overlap_ratio_old=1.0,
        overlap_ratio_new=1.0,
        warnings=(),
    )
    return ManifestDiffReport(
        summary=summary,
        added=tuple(_entry(path, "added") for path in added),
        removed=tuple(_entry(path, "removed") for path in removed),
        changed=tuple(_entry(path, "changed") for path in changed),
        unchanged=tuple(_entry(path, "unchanged") for path in unchanged),
        moved=tuple(
            ManifestMovedEntry(old_path=old, new_path=new, size=4, chunk_digest="d") for old, new in moved
        ),
    )


def _actions(plan) -> dict[str, FileAction]:
    return {entry.path: entry.action for entry in plan.entries}


def test_five_status_mapping(tmp_path: Path):
    files = [_make_file("keep.bin"), _make_file("patch.bin"), _make_file("new.bin"), _make_file("moved-new.bin")]
    report = _report(
        unchanged=("keep.bin",),
        changed=("patch.bin",),
        added=("new.bin", "moved-new.bin"),
        removed=("gone.bin", "moved-old.bin"),
        moved=(("moved-old.bin", "moved-new.bin"),),
    )

    plan = build_update_plan(report, files)
    actions = _actions(plan)

    assert actions["keep.bin"] == FileAction.SKIP
    assert actions["patch.bin"] == FileAction.PATCH
    assert actions["new.bin"] == FileAction.NEW
    assert actions["moved-new.bin"] == FileAction.MOVE
    assert actions["gone.bin"] == FileAction.REMOVE
    # moved 的旧路径不应再出现在 REMOVE。
    assert "moved-old.bin" not in actions

    move_entry = next(entry for entry in plan.entries if entry.action == FileAction.MOVE)
    assert move_entry.move_from == "moved-old.bin"


def test_none_report_falls_back_to_patch():
    files = [_make_file("a.bin"), _make_file("b.lnk", link="target")]

    plan = build_update_plan(None, files)
    actions = _actions(plan)

    assert actions["a.bin"] == FileAction.PATCH
    assert actions["b.lnk"] == FileAction.SKIP
    assert not [entry for entry in plan.entries if entry.action == FileAction.REMOVE]


def test_files_subset_restricts_plan():
    files = [_make_file("only.bin")]
    report = _report(changed=("only.bin", "other.bin"), removed=("gone.bin",))

    plan = build_update_plan(report, files)
    paths = {entry.path for entry in plan.entries}

    # other.bin 不在目标文件集内，不进计划；REMOVE 仍以报告为准。
    assert paths == {"only.bin", "gone.bin"}


def test_path_missing_from_report_defaults_to_patch():
    files = [_make_file("mystery.bin")]
    report = _report(unchanged=("known.bin",))

    plan = build_update_plan(report, files)

    assert _actions(plan)["mystery.bin"] == FileAction.PATCH


def test_link_file_is_skip_even_when_changed():
    files = [_make_file("a.lnk", link="target")]
    report = _report(changed=("a.lnk",))

    plan = build_update_plan(report, files)

    assert _actions(plan)["a.lnk"] == FileAction.SKIP


def test_by_action_helper():
    files = [_make_file("a.bin"), _make_file("b.bin")]
    report = _report(changed=("a.bin",), unchanged=("b.bin",))

    plan = build_update_plan(report, files)

    assert [entry.path for entry in plan.by_action(FileAction.PATCH)] == ["a.bin"]
    assert [entry.path for entry in plan.by_action(FileAction.SKIP)] == ["b.bin"]
