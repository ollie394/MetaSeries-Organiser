"""Offline regressions for planning, exclusive transfers, and recovery.

All files and settings are isolated in pytest's temporary directory. Providers
are local fixtures and HTTP is blocked, so these tests never need credentials.
"""

import errno
import base64
import json
from pathlib import Path
import sys
import threading
import xml.etree.ElementTree as ET

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import metaseries as mso

REAL_USER_STATE_ROOT = mso.user_state_root
REAL_STATE_ROOT = mso.state_root


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(mso, "app_root", lambda: tmp_path / "app")
    monkeypatch.setattr(mso, "user_state_root", lambda: tmp_path / "profile")
    monkeypatch.setattr(mso, "state_root", lambda portable=None: tmp_path / "state")
    monkeypatch.setattr(mso, "_PORTABLE_OVERRIDE", False)
    monkeypatch.delenv("MSO_PORTABLE", raising=False)
    monkeypatch.delenv("MSO_STATE_DIR", raising=False)

    def block_http(*args, **kwargs):
        pytest.fail("Core tests must not contact a metadata provider")

    monkeypatch.setattr(mso.requests.sessions.Session, "request", block_http)


@pytest.fixture
def episode():
    return mso.EpisodeInfo(
        1, 2, "The Arrival", "2026-10-02", absolute=14,
        dvd_season=2, dvd_episode=3, overview="A <visitor> & a mystery.",
        provider_id="episode-2",
    )


@pytest.fixture
def context(episode):
    class OfflineProvider(mso.Provider):
        def build_episode_index(self, show_id, order="aired"):
            return [episode]

    return mso.ShowContext("Example Show", 2026, "offline", OfflineProvider(),
                           "show-1", [episode])


def make_plan(tmp_path, episode, *, sidecar=False, payload=b"original video bytes\0\xff"):
    source = tmp_path / "source" / "Example.Show.S01E02.mkv"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(payload)
    destination = tmp_path / "library" / "episode.mkv"
    item = mso.PlanItem(source, destination, destination.with_suffix(".nfo") if sidecar else None,
                        "test transfer", episode=episode, episodes=[episode],
                        show_title="Example Show", year=2026, sidecar=sidecar)
    return mso.PlanList([item]), source, destination


@pytest.mark.parametrize("filename, expected", [
    ("Show.S01E02.1080p.mkv", ("se", 1, 2, None, None)),
    ("Show.s00e03.mkv", ("special", 0, 3, None, None)),
    ("Show.S02E04-E06.mkv", ("se_range", 2, 4, 6, None)),
    ("Show.1x09.mkv", ("se", 1, 9, None, None)),
    ("News.2026.10.02.mkv", ("date", None, None, None, "2026-10-02")),
    ("Example Show (2026) - 14.mkv", ("se", 1, 14, None, None)),
    ("014 - The Arrival.mkv", ("absolute", None, 14, None, None)),
    ("News.2026.02.30.mkv", ("unknown", None, None, None, None)),
])
def test_parse_common_filename_formats(filename, expected):
    parsed = mso.parse_filename(filename)
    assert (parsed.kind, parsed.season, parsed.ep1, parsed.ep2, parsed.date_iso) == expected


def test_parser_keeps_extra_and_split_markers():
    parsed = mso.parse_filename("Example.S01E02-trailer2-part1.mkv")
    assert (parsed.extra_type, parsed.extra_index) == ("trailer", 2)
    assert (parsed.split_token, parsed.split_index) == ("part", 1)


@pytest.mark.parametrize("options, expected_code", [
    ({}, "S01E02"),
    ({"upper_se": False}, "s01e02"),
    ({"order": "dvd"}, "S02E03"),
    ({"order": "absolute"}, "014"),
    ({"multi_ep": (2, 3)}, "S01E02-E03"),
    ({"order": "absolute", "multi_ep": (14, 15)}, "014-015"),
    ({"date_iso": "2026-10-02"}, "02-10-2026"),
])
def test_output_filename_preserves_chosen_numbering(episode, options, expected_code):
    name = mso.target_filename("Example Show", 2026, episode, ".MKV", **options)
    assert name == f"Example Show (2026) - {expected_code} - The Arrival.mkv"


def test_absolute_output_rejects_missing_mapping(episode):
    episode.absolute = None
    with pytest.raises(ValueError, match="absolute"):
        mso.target_filename("Example", None, episode, ".mkv", order="absolute")


@pytest.mark.parametrize("value, expected", [
    ("CON", "_CON"), ("NUL.txt", "_NUL.txt"),
    (' Bad:/Name?*<>|". ', "Bad__Name______"),
    ("...", "Untitled"),
])
def test_filename_sanitization_is_windows_safe(value, expected):
    assert mso.sanitize_filename(value) == expected


def test_long_names_keep_episode_and_extra_suffixes(tmp_path, episode):
    name = mso.target_filename("A" * 200, 2026, episode, ".mkv",
                               extra="trailer2", split="part1")
    result = mso.safe_destination(tmp_path, name, collision_suffix=" (2)")
    assert "S01E02" in result.name
    assert result.name.endswith("-trailer2-part1 (2).mkv")
    assert len(str(result).encode("utf-16-le")) // 2 <= 240


def test_single_nfo_is_valid_xml_and_escapes_metadata(episode):
    tree = ET.fromstring(mso.nfo_bytes([episode], "Show <&>", 2026))
    assert tree.findtext("showtitle") == "Show <&>"
    assert tree.findtext("plot") == "A <visitor> & a mystery."
    assert tree.findtext("title") == "The Arrival"


@pytest.mark.parametrize("order, season, number", [("dvd", "2", "3"), ("absolute", "1", "14")])
def test_nfo_uses_selected_numbering(episode, order, season, number):
    tree = ET.fromstring(mso.nfo_bytes([episode], "Example", None, order))
    assert (tree.findtext("season"), tree.findtext("episode")) == (season, number)


def test_write_nfo_does_not_overwrite_existing_sidecar(tmp_path, episode):
    sidecar = tmp_path / "existing.nfo"
    sidecar.write_text("user metadata", encoding="utf-8")
    with pytest.raises(FileExistsError):
        mso.write_nfo(sidecar, episode, "Example", 2026)
    assert sidecar.read_text(encoding="utf-8") == "user metadata"


def test_planner_skips_missing_mappings_and_non_video_files(tmp_path, context):
    source = tmp_path / "source"
    source.mkdir()
    (source / "Example.S01E02.mkv").write_bytes(b"video")
    (source / "Example.S01E99.mkv").write_bytes(b"unmapped")
    (source / "readme.txt").write_text("leave me alone", encoding="utf-8")
    plan = mso.plan_moves(context, source, tmp_path / "output", lambda message: None, True, False)
    assert len(plan) == 1
    assert plan.skipped_count == 2
    assert plan[0].dst.name == "Example Show (2026) - S01E02 - The Arrival.mkv"
    assert sorted(path.name for path in source.iterdir()) == [
        "Example.S01E02.mkv", "Example.S01E99.mkv", "readme.txt"]
    assert not (tmp_path / "output").exists()


def test_planner_never_partially_maps_multi_episode_file(tmp_path, context):
    (tmp_path / "Example.S01E02-E03.mkv").write_bytes(b"two episodes")
    plan = mso.plan_moves(context, tmp_path, None, lambda message: None, True, False)
    assert not plan
    assert plan.skipped_count == 1


def test_recursive_planning_excludes_nested_output(tmp_path, context):
    source = tmp_path / "source"
    nested = source / "incoming"
    nested.mkdir(parents=True)
    (nested / "Example.S01E02.mkv").write_bytes(b"incoming")
    output = source / "output"
    output.mkdir()
    (output / "Example.S01E02.mkv").write_bytes(b"already processed")
    plan = mso.plan_moves(context, source, output, lambda message: None, True, False, recursive=True)
    assert [item.src for item in plan] == [nested / "Example.S01E02.mkv"]


def test_ambiguous_title_requires_explicit_resolution(tmp_path, context, episode):
    context.episode_index = [episode, mso.EpisodeInfo(1, 3, "The Arrival")]
    (tmp_path / "Example Show - The Arrival.mkv").write_bytes(b"ambiguous")
    plan = mso.plan_moves(context, tmp_path, tmp_path / "output", lambda message: None, True, False)
    assert len(plan) == 1
    assert not plan[0].resolved
    assert not plan[0].approved
    assert len(plan[0].candidates) == 2


def test_plan_reserves_unique_names_and_preserves_existing_sidecars(tmp_path, episode):
    plan, source, destination = make_plan(tmp_path, episode, sidecar=True)
    destination.parent.mkdir(parents=True)
    existing_sidecar = destination.with_suffix(".nfo")
    existing_sidecar.write_bytes(b"existing metadata")
    second_source = source.with_name("second.mkv")
    second_source.write_bytes(b"other video")
    plan.append(mso.PlanItem(second_source, destination, None, "second", sidecar=True))
    mso.finalize_plan_destinations(plan)
    assert plan[0].dst != destination
    assert plan[0].dst != plan[1].dst
    assert all(item.conflict for item in plan)
    assert existing_sidecar.read_bytes() == b"existing metadata"


@pytest.mark.parametrize("operation", ["copy", "move"])
@pytest.mark.parametrize("verification", [None, "SHA-256", "BLAKE3", "XXH3-128", "quick-sampled", "quick-identity", "Skip verification"])
def test_transfer_and_undo_round_trip(tmp_path, episode, operation, verification):
    plan, source, destination = make_plan(tmp_path, episode, sidecar=True)
    payload = source.read_bytes()
    manifest = mso.execute_plan(plan, lambda message: None, operation=operation, verification=verification)
    saved = json.loads(manifest.read_text(encoding="utf-8"))
    assert saved["failure_count"] == 0
    assert destination.read_bytes() == payload
    assert destination.with_suffix(".nfo").exists()
    assert source.exists() == (operation == "copy")
    if operation == "copy":
        source.write_bytes(b"source edited after copy")
        assert destination.read_bytes() == payload, "Copy must be independent of its source"
    assert mso.undo_from_manifest(manifest, lambda message: None) == 0
    assert not destination.exists()
    assert not destination.with_suffix(".nfo").exists()
    assert source.read_bytes() == (b"source edited after copy" if operation == "copy" else payload)
    assert json.loads(manifest.read_text(encoding="utf-8"))["undone"] is True
    assert mso.undo_from_manifest(manifest, lambda message: None) == 0


@pytest.mark.parametrize("operation", ["copy", "move"])
@pytest.mark.parametrize("verification", [None, "SHA-256"])
def test_undo_preserves_modified_destination(tmp_path, episode, operation, verification):
    plan, source, destination = make_plan(tmp_path, episode)
    manifest = mso.execute_plan(plan, lambda message: None, operation=operation, verification=verification)
    destination.write_bytes(b"user edited this file")
    assert mso.undo_from_manifest(manifest, lambda message: None) == 1
    assert destination.read_bytes() == b"user edited this file"
    assert source.exists() == (operation == "copy")
    assert not json.loads(manifest.read_text(encoding="utf-8"))["undone"]


@pytest.mark.parametrize("verification", [None, "SHA-256"])
def test_undo_move_never_overwrites_new_file_at_original_path(tmp_path, episode, verification):
    plan, source, destination = make_plan(tmp_path, episode)
    payload = source.read_bytes()
    manifest = mso.execute_plan(plan, lambda message: None, verification=verification)
    source.write_bytes(b"new unrelated download")
    assert mso.undo_from_manifest(manifest, lambda message: None) == 1
    assert source.read_bytes() == b"new unrelated download"
    assert destination.read_bytes() == payload


@pytest.mark.parametrize("operation", ["copy", "move"])
def test_destination_created_after_review_is_never_overwritten(tmp_path, episode, operation):
    plan, source, destination = make_plan(tmp_path, episode)
    payload = source.read_bytes()
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"concurrent file")
    manifest = mso.execute_plan(plan, lambda message: None, operation=operation, verification="SHA-256")
    assert json.loads(manifest.read_text(encoding="utf-8"))["failure_count"] == 1
    assert source.read_bytes() == payload
    assert destination.read_bytes() == b"concurrent file"
    assert mso.undo_from_manifest(manifest, lambda message: None) == 0
    assert destination.read_bytes() == b"concurrent file"


@pytest.mark.parametrize("excluded_state", ["unapproved", "unresolved", "no_op", "same_path"])
def test_execution_only_changes_explicitly_approved_transfers(tmp_path, episode, excluded_state):
    plan, source, destination = make_plan(tmp_path, episode)
    payload = source.read_bytes()
    if excluded_state == "unapproved":
        plan[0].approved = False
    elif excluded_state == "unresolved":
        plan[0].resolved = False
    elif excluded_state == "no_op":
        plan[0].no_op = True
    else:
        plan[0].dst = source
    manifest = mso.execute_plan(plan, lambda message: None, verification="SHA-256")
    assert source.read_bytes() == payload
    assert not destination.exists()
    assert json.loads(manifest.read_text(encoding="utf-8"))["items"] == []


def test_cross_volume_move_fallback_and_recovery(tmp_path, episode, monkeypatch):
    def unavailable_link(*args, **kwargs):
        raise OSError(errno.EXDEV, "simulated different volumes")

    plan, source, destination = make_plan(tmp_path, episode)
    payload = source.read_bytes()
    monkeypatch.setattr(mso.os, "link", unavailable_link)
    manifest = mso.execute_plan(plan, lambda message: None, verification="SHA-256")
    assert destination.read_bytes() == payload
    assert not source.exists()
    assert mso.undo_from_manifest(manifest, lambda message: None) == 0
    assert source.read_bytes() == payload
    assert not destination.exists()


def test_changed_source_is_rejected_before_transfer(tmp_path, episode):
    plan, source, destination = make_plan(tmp_path, episode)
    record = {"source_snapshot": mso.file_snapshot(source), "verification": "SHA-256"}
    changed_payload = source.read_bytes() + b"changed since snapshot"
    source.write_bytes(changed_payload)
    with pytest.raises(OSError, match="changed"):
        mso.optimized_transfer(source, destination, record, lambda *args: None, move=True)
    assert source.read_bytes() == changed_payload
    assert not destination.exists()


def test_source_changed_during_move_is_retained(tmp_path, episode):
    plan, source, destination = make_plan(tmp_path, episode)
    changed = False

    def progress(done, total, phase):
        nonlocal changed
        if not changed and phase.startswith("Verifying:"):
            changed = True
            source.write_bytes(b"user changed the source during transfer")

    manifest = mso.execute_plan(plan, lambda message: None, progress=progress, verification="SHA-256")
    assert changed
    assert source.read_bytes() == b"user changed the source during transfer"
    assert json.loads(manifest.read_text(encoding="utf-8"))["failure_count"] == 1


def test_cancellation_before_transfer_leaves_files_untouched(tmp_path, episode):
    plan, source, destination = make_plan(tmp_path, episode)
    payload = source.read_bytes()
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(mso.Cancelled):
        mso.execute_plan(plan, lambda message: None, cancelled, verification="SHA-256")
    assert source.read_bytes() == payload
    assert not destination.exists()


def test_cancellation_during_copy_keeps_source_and_recovery_record(tmp_path, episode):
    payload = b"v" * (mso.TRANSFER_BLOCK * 2 + 7)
    plan, source, destination = make_plan(tmp_path, episode, payload=payload)
    cancelled = threading.Event()

    def progress(done, total, phase):
        if phase.startswith("Copying:"):
            cancelled.set()

    with pytest.raises(mso.Cancelled):
        mso.execute_plan(plan, lambda message: None, cancelled, progress,
                         operation="copy", verification="SHA-256")
    assert source.read_bytes() == payload
    manifests = list((mso.state_root() / "manifests").glob("manifest-*.json"))
    assert len(manifests) == 1
    saved = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert saved["cancelled"] is True
    assert saved["items"][0]["destination_identity"] == mso.file_identity(destination)
    assert saved["items"][0]["status"] == "copying"


def test_undo_preserves_modified_sidecar(tmp_path, episode):
    plan, source, destination = make_plan(tmp_path, episode, sidecar=True)
    manifest = mso.execute_plan(plan, lambda message: None, operation="copy", verification="SHA-256")
    sidecar = destination.with_suffix(".nfo")
    sidecar.write_bytes(b"edited metadata")
    assert mso.undo_from_manifest(manifest, lambda message: None) == 1
    assert sidecar.read_bytes() == b"edited metadata"
    assert source.exists()
    assert not destination.exists()


@pytest.mark.parametrize("label", ["api_key", "access_token", "refresh_token", "token", "pin", "password", "Authorization"])
def test_log_redaction_hides_credentials(label):
    assert "example-private-value" not in mso.redact_secrets(f"{label}=example-private-value")


def test_log_redaction_hides_bearer_and_query_credentials():
    result = mso.redact_secrets("Bearer example-private-value https://example.invalid/?api_key=other-secret&language=en")
    assert "example-private-value" not in result
    assert "other-secret" not in result
    assert "language=en" in result


def test_settings_round_trip_uses_isolated_state():
    settings = dict(mso.DEFAULT_SETTINGS, preferred_language="fr", operation="copy")
    mso.save_settings(settings)
    loaded = mso.load_settings()
    assert loaded["preferred_language"] == "fr"
    assert loaded["operation"] == "copy"
    assert mso.settings_path().is_relative_to(mso.state_root())


def test_corrupt_settings_fall_back_to_defaults():
    path = mso.settings_path()
    path.parent.mkdir(parents=True)
    path.write_text("{incomplete json", encoding="utf-8")
    assert mso.load_settings()["operation"] == mso.DEFAULT_SETTINGS["operation"]


@pytest.mark.parametrize("fallback_config", [False, True])
def test_legacy_credentials_are_never_loaded_from_settings(fallback_config):
    path = mso.app_root() / "config" / "settings.json" if fallback_config else mso.settings_path()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"preferred_language": "fr", "cred_tvdb_token": "example-token",
                                "cred_tmdb_key": "example-key", "cred_future_secret": "example-secret"}),
                    encoding="utf-8")
    loaded = mso.load_settings()
    assert loaded["preferred_language"] == "fr"
    assert not any(key.startswith("cred_") for key in loaded)


@pytest.mark.parametrize("portable", [False, True])
def test_legacy_credentials_are_never_saved_to_settings(monkeypatch, portable):
    monkeypatch.setattr(mso, "portable_mode_enabled", lambda: portable)
    mso.save_settings(dict(mso.DEFAULT_SETTINGS, operation="copy", cred_tvdb_token="example-token",
                           cred_tvdb_pin="example-pin", cred_tmdb_key="example-key"))
    paths = [mso.settings_path()]
    if portable:
        paths.append(mso.user_state_root() / "config" / "settings.json")
    for path in paths:
        saved = path.read_text(encoding="utf-8")
        assert json.loads(saved)["operation"] == "copy"
        assert "cred_" not in saved
        assert "example-token" not in saved
        assert "example-pin" not in saved
        assert "example-key" not in saved


def test_portable_vault_encrypts_and_reloads_credentials():
    store = mso.CredentialsStore(portable=True)
    store.set_session_passphrase("a test passphrase with several words")
    assert store.set("tvdb_token", "example-private-value") is True
    assert store.set("tmdb_key", "second-example-private-value") is True
    encrypted = store.vault_path.read_bytes()
    assert b"example-private-value" not in encrypted
    assert b"example-private-value" not in base64.b64decode(encrypted)
    assert b"a test passphrase with several words" not in encrypted
    reopened = mso.CredentialsStore(portable=True)
    reopened.set_session_passphrase("a test passphrase with several words")
    assert reopened.get("tvdb_token") == "example-private-value"
    assert reopened.get("tmdb_key") == "second-example-private-value"
    assert reopened.get("not-saved") is None


def test_portable_vault_requires_passphrase_before_saving():
    store = mso.CredentialsStore(portable=True)
    with pytest.raises(RuntimeError, match="passphrase"):
        store.set("tvdb_token", "example-private-value")
    assert not store.vault_path.exists()


def test_incorrect_vault_passphrase_cannot_read_or_replace_credentials():
    store = mso.CredentialsStore(portable=True)
    store.set_session_passphrase("correct test passphrase")
    store.set("tvdb_token", "example-private-value")
    original = store.vault_path.read_bytes()
    reopened = mso.CredentialsStore(portable=True)
    reopened.set_session_passphrase("incorrect test passphrase")
    with pytest.raises(RuntimeError, match="incorrect"):
        reopened.get("tvdb_token")
    with pytest.raises(RuntimeError, match="incorrect"):
        reopened.set("tvdb_token", "should-never-replace-value")
    assert store.vault_path.read_bytes() == original
    reopened.set_session_passphrase("correct test passphrase")
    assert reopened.get("tvdb_token") == "example-private-value"


def test_vault_passphrase_change_clears_previously_unlocked_credentials():
    store = mso.CredentialsStore(portable=True)
    store.set_session_passphrase("correct test passphrase")
    store.set("tvdb_token", "example-private-value")
    assert store.get("tvdb_token") == "example-private-value"
    store.set_session_passphrase("incorrect test passphrase")
    with pytest.raises(RuntimeError, match="incorrect"):
        store.get("tvdb_token")


def test_portable_vault_detects_tampered_ciphertext():
    store = mso.CredentialsStore(portable=True)
    store.set_session_passphrase("test passphrase")
    store.set("tvdb_token", "example-private-value")
    damaged = bytearray(base64.b64decode(store.vault_path.read_bytes()))
    damaged[-1] ^= 1
    store.vault_path.write_bytes(base64.b64encode(damaged))
    reopened = mso.CredentialsStore(portable=True)
    reopened.set_session_passphrase("test passphrase")
    with pytest.raises(RuntimeError, match="damaged"):
        reopened.get("tvdb_token")


def test_keyring_failure_uses_memory_without_claiming_persistence(monkeypatch):
    class FailingKeyring:
        @staticmethod
        def set_password(*args):
            raise RuntimeError("simulated unavailable credential store")

        @staticmethod
        def get_password(*args):
            pytest.fail("A session credential should not need a keyring read")

    monkeypatch.setitem(sys.modules, "keyring", FailingKeyring)
    store = mso.CredentialsStore(portable=False)
    assert store.set("tvdb_token", "example-private-value") is False
    assert store.get("tvdb_token") == "example-private-value"
    assert not store.vault_path.exists()


@pytest.mark.parametrize("portable", [None, False, True])
def test_explicit_state_directory_contains_all_application_state(tmp_path, monkeypatch, portable):
    explicit = tmp_path / "explicit-state"
    monkeypatch.setenv("MSO_STATE_DIR", str(explicit))
    monkeypatch.setattr(mso, "user_state_root", REAL_USER_STATE_ROOT)
    monkeypatch.setattr(mso, "state_root", REAL_STATE_ROOT)
    monkeypatch.setattr(mso, "_PORTABLE_OVERRIDE", bool(portable))
    assert mso.state_root(portable) == explicit.resolve()
    assert mso.user_state_root() == explicit.resolve()
    mso.check_state_writable(portable)
    mso.save_settings(dict(mso.DEFAULT_SETTINGS, operation="copy"))
    store = mso.CredentialsStore(portable=True)
    store.set_session_passphrase("test passphrase")
    store.set("tvdb_token", "example-private-value")
    assert mso.settings_path().is_relative_to(explicit)
    assert store.vault_path.is_relative_to(explicit)
    assert mso.manifests_dir().is_relative_to(explicit)
    assert mso.load_settings()["operation"] == "copy"
    assert not (mso.app_root() / "data").exists()
