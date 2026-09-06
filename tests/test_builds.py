"""builds.py: single-file builds download raw (an .apk installs with no
unzip), multi-file builds still come down as a zip."""
import json
import zipfile

import pytest

from gremlin_core import builds


@pytest.fixture
def downloads(tmp_path, monkeypatch):
    d = tmp_path / "Downloads"
    d.mkdir()
    monkeypatch.setattr(builds, "BUILDS_DIR", d)
    return d


def _make_build(downloads, name, files: dict):
    folder = downloads / name
    folder.mkdir()
    (folder / builds.MARKER).write_text(json.dumps({"goal": f"{name} goal", "created": 1.0}))
    for fname, content in files.items():
        (folder / fname).write_bytes(content if isinstance(content, bytes) else content.encode())
    return folder


def test_single_file_build_lists_single_file_and_downloads_raw(downloads):
    _make_build(downloads, "app_update", {"gremlin.apk": b"PK\x03\x04 fake apk bytes"})

    listed = {b["name"]: b for b in builds.list_builds()}
    assert listed["app_update"]["single_file"] == "gremlin.apk"

    data, filename, mime = builds.read_single_file("app_update")
    assert filename == "gremlin.apk"
    assert mime == "application/vnd.android.package-archive"
    assert data == b"PK\x03\x04 fake apk bytes"


def test_multi_file_build_has_no_single_file_and_zips(downloads):
    _make_build(downloads, "proj", {"main.py": "print(1)\n", "readme.md": "hi\n"})

    listed = {b["name"]: b for b in builds.list_builds()}
    assert listed["proj"]["single_file"] == ""
    assert builds.read_single_file("proj") is None

    zbytes, zname = builds.make_zip("proj")
    assert zname == "proj.zip"
    names = zipfile.ZipFile(__import__("io").BytesIO(zbytes)).namelist()
    assert "proj/main.py" in names and "proj/readme.md" in names


def test_marker_not_counted_as_the_single_file(downloads):
    # a build folder always has the marker -- it must not be what
    # read_single_file hands back
    _make_build(downloads, "just_marker", {})
    assert builds.read_single_file("just_marker") is None


def test_script_single_file_gets_its_real_name_and_mime(downloads):
    _make_build(downloads, "backup_tool", {"backup.sh": "#!/bin/sh\necho hi\n"})
    data, filename, mime = builds.read_single_file("backup_tool")
    assert filename == "backup.sh" and mime == "text/x-shellscript"


def test_unknown_build_returns_none(downloads):
    assert builds.read_single_file("nope") is None
