"""diff 报告驱动的更新计划器单测."""

from pathlib import Path

import pytest

from riotmanifest.diff.manifest_diff import (
    ManifestDiffEntry,
    ManifestDiffReport,
    ManifestDiffSummary,
    ManifestMovedEntry,
)
from riotmanifest.manifest import PatcherFile
from riotmanifest.update.planner import FileAction, build_update_plan


def _make_file(name: str, *, link: str = "", size: int = 4) -> PatcherFile:
    return PatcherFile(
        name=name,
        size=size,
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
        moved=tuple(ManifestMovedEntry(old_path=old, new_path=new, size=4, chunk_digest="d") for old, new in moved),
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

    (tmp_path / "keep.bin").write_bytes(b"keep")
    (tmp_path / "moved-old.bin").write_bytes(b"move")
    plan = build_update_plan(
        report,
        files,
        managed_files={"keep.bin", "gone.bin", "moved-old.bin"},
        output_dir=tmp_path,
    )
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

    plan = build_update_plan(report, files, managed_files={"gone.bin"})
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


def test_by_action_helper(tmp_path: Path):
    files = [_make_file("a.bin"), _make_file("b.bin")]
    report = _report(changed=("a.bin",), unchanged=("b.bin",))

    (tmp_path / "b.bin").write_bytes(b"data")
    plan = build_update_plan(report, files, managed_files={"b.bin"}, output_dir=tmp_path)

    assert [entry.path for entry in plan.by_action(FileAction.PATCH)] == ["a.bin"]
    assert [entry.path for entry in plan.by_action(FileAction.SKIP)] == ["b.bin"]


def test_managed_actions_require_membership_and_disk_gate(tmp_path: Path):
    files = [_make_file("keep.bin"), _make_file("moved-new.bin")]
    report = _report(
        unchanged=("keep.bin",),
        added=("moved-new.bin",),
        removed=("gone.bin", "moved-old.bin", "unmanaged.bin"),
        moved=(("moved-old.bin", "moved-new.bin"),),
    )
    (tmp_path / "keep.bin").write_bytes(b"bad")
    (tmp_path / "moved-old.bin").write_bytes(b"move")

    plan = build_update_plan(
        report,
        files,
        managed_files={"keep.bin", "gone.bin"},
        output_dir=tmp_path,
    )
    actions = _actions(plan)

    assert actions["keep.bin"] == FileAction.PATCH
    assert actions["moved-new.bin"] == FileAction.PATCH
    assert actions["gone.bin"] == FileAction.REMOVE
    assert "unmanaged.bin" not in actions


def test_symlink_cannot_authorize_skip_or_move(tmp_path: Path):
    real = tmp_path / "real.bin"
    real.write_bytes(b"data")
    keep_link = tmp_path / "keep.bin"
    move_link = tmp_path / "moved-old.bin"
    try:
        keep_link.symlink_to(real)
        move_link.symlink_to(real)
    except OSError as exc:
        pytest.skip(f"当前环境不允许创建符号链接: {exc}")

    files = [_make_file("keep.bin"), _make_file("moved-new.bin")]
    report = _report(
        unchanged=("keep.bin",),
        added=("moved-new.bin",),
        removed=("moved-old.bin",),
        moved=(("moved-old.bin", "moved-new.bin"),),
    )
    plan = build_update_plan(
        report,
        files,
        managed_files={"keep.bin", "moved-old.bin"},
        output_dir=tmp_path,
    )

    actions = _actions(plan)
    assert actions["keep.bin"] == FileAction.PATCH
    assert actions["moved-new.bin"] == FileAction.PATCH
