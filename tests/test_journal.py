"""The undo path is the last line of defence, so it gets tested first."""

from __future__ import annotations

from ecs.journal import Entry, Journal


def make_journal(tmp_path):
    return Journal(path=tmp_path / "journal.jsonl")


def test_uncommitted_entries_are_excluded(tmp_path):
    j = make_journal(tmp_path)
    j.record(Entry(op="trash", message_id="m1", before={"labelIds": ["INBOX"]}))
    # Recorded but never committed — a crash mid-wave looks like this.
    assert j.committed_entries() == []
    assert j.undo_plan() == []


def test_label_modify_inverts_add_and_remove(tmp_path):
    j = make_journal(tmp_path)
    entry = Entry(
        op="modify",
        message_id="m1",
        wave=1,
        before={"labelIds": ["INBOX", "UNREAD"]},
        after={"addLabelIds": ["Label_9"], "removeLabelIds": ["INBOX"]},
    )
    j.record(entry)
    j.commit(entry)

    (inverse,) = j.undo_plan()
    assert inverse["op"] == "modify"
    assert inverse["message_id"] == "m1"
    # What we added, we remove; what we removed, we add back.
    assert inverse["addLabelIds"] == ["INBOX"]
    assert inverse["removeLabelIds"] == ["Label_9"]


def test_trash_inverts_to_untrash_with_original_labels(tmp_path):
    j = make_journal(tmp_path)
    entry = Entry(
        op="trash",
        message_id="m2",
        wave=1,
        before={"labelIds": ["INBOX", "CATEGORY_PROMOTIONS"]},
    )
    j.record(entry)
    j.commit(entry)

    (inverse,) = j.undo_plan()
    assert inverse["op"] == "untrash"
    assert inverse["message_id"] == "m2"
    assert inverse["restore_labels"] == ["INBOX", "CATEGORY_PROMOTIONS"]


def test_undo_is_newest_first_so_labels_outlive_their_messages(tmp_path):
    """Labels are created before use, so they must be deleted after the revert."""
    j = make_journal(tmp_path)
    create = Entry(op="create_label", after={"label_id": "L1", "name": "Finance"})
    use = Entry(
        op="modify",
        message_id="m3",
        after={"addLabelIds": ["L1"], "removeLabelIds": ["INBOX"]},
    )
    for e in (create, use):
        j.record(e)
        j.commit(e)

    plan = j.undo_plan()
    assert [step["op"] for step in plan] == ["modify", "delete_label"]


def test_unsubscribe_is_reported_as_irreversible(tmp_path):
    j = make_journal(tmp_path)
    entry = Entry(
        op="unsubscribe",
        cluster_key="list:deals@example.com",
        after={"endpoint": "https://example.com/u/abc", "method": "one_click"},
    )
    j.record(entry)
    j.commit(entry)

    (inverse,) = j.undo_plan()
    assert inverse["op"] == "noop"
    assert "cannot be reversed" in inverse["reason"]


def test_failed_mutation_is_not_undone(tmp_path):
    j = make_journal(tmp_path)
    entry = Entry(op="trash", message_id="m4", before={"labelIds": ["INBOX"]})
    j.record(entry)
    j.commit(entry, error="403 insufficient permission")
    # It never landed, so there is nothing to reverse.
    assert j.undo_plan() == []


def test_wave_and_since_filters(tmp_path):
    j = make_journal(tmp_path)
    for wave in (1, 2):
        e = Entry(op="trash", message_id=f"w{wave}", wave=wave, before={"labelIds": []})
        j.record(e)
        j.commit(e)

    assert j.waves() == [1, 2]
    assert j.last_wave() == 2
    (only_wave_two,) = j.undo_plan(wave=2)
    assert only_wave_two["message_id"] == "w2"


def test_torn_final_line_does_not_break_reading(tmp_path):
    j = make_journal(tmp_path)
    entry = Entry(op="trash", message_id="m5", before={"labelIds": ["INBOX"]})
    j.record(entry)
    j.commit(entry)
    # Simulate a hard kill mid-write.
    with j.path.open("a", encoding="utf-8") as fh:
        fh.write('{"op": "trash", "message_i')

    assert len(j.committed_entries()) == 1
    assert len(j.undo_plan()) == 1
