import io
import json
import sys
import tempfile
import zipfile
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageCms

sys.path.insert(0, str(Path(__file__).parent.parent))
import web


@pytest.fixture
def client():
    return web.app.test_client()


def png_bytes(array=None, **options):
    if array is None:
        array = np.arange(32 * 32 * 3, dtype=np.uint32).reshape(32, 32, 3).astype(np.uint8)
    buffer = io.BytesIO()
    Image.fromarray(array).save(buffer, format="PNG", **options)
    return buffer.getvalue()


def upload(client, path, data, name="photo.png"):
    return client.post(path, data={"file": (io.BytesIO(data), name)}, content_type="multipart/form-data")


def test_home_page_is_served_with_security_headers(client):
    response = client.get("/")
    assert response.status_code == 200
    assert b"Turn any file into sound" in response.data
    assert "default-src 'none'" in response.headers["Content-Security-Policy"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.parametrize("path", ["/style.css", "/app.js", "/motion.js"])
def test_page_assets_are_served(client, path):
    assert client.get(path).status_code == 200


def test_encode_then_decode_restores_pixels_and_profile(client):
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    source = png_bytes(icc_profile=profile)
    encoded = upload(client, "/api/encode", source)
    assert encoded.status_code == 200
    assert encoded.mimetype == "audio/flac"
    result = json.loads(encoded.headers["X-Filetape-Result"])
    assert result["before"] == len(source)
    assert result["after"] == len(encoded.data)
    assert result["icc_profile"] is True

    decoded = upload(client, "/api/decode", encoded.data, "photo.flac")
    assert decoded.status_code == 200
    assert decoded.mimetype == "image/png"
    restored = Image.open(io.BytesIO(decoded.data))
    assert np.array_equal(np.asarray(restored), np.asarray(Image.open(io.BytesIO(source))))
    assert restored.info["icc_profile"] == profile


@pytest.mark.parametrize("name, data", [
    ("notes.txt", b"plain text " * 500),
    ("report.final.pdf", bytes(range(256)) * 20),
    ("song.mp3", b"ID3" + bytes(5000)),
    ("empty.dat", b""),
    ("Résumé 2026.docx", b"text"),
])
def test_any_file_comes_back_with_its_name(client, name, data):
    encoded = upload(client, "/api/encode", data, name)
    assert encoded.status_code == 200
    assert encoded.mimetype == "audio/flac"
    assert json.loads(encoded.headers["X-Filetape-Result"])["name"] == name
    decoded = upload(client, "/api/decode", encoded.data, "audio.flac")
    assert decoded.status_code == 200
    assert decoded.data == data
    assert json.loads(decoded.headers["X-Filetape-Result"])["name"] == name


def test_hostile_stored_name_is_reduced_to_a_file_name(client):
    encoded = upload(client, "/api/encode", b"payload " * 100, "..\\..\\evil.txt")
    decoded = upload(client, "/api/decode", encoded.data, "audio.flac")
    assert json.loads(decoded.headers["X-Filetape-Result"])["name"] == "evil.txt"


def test_not_audio_gives_a_clean_error(client):
    response = upload(client, "/api/decode", b"plain text", "notes.flac")
    assert response.status_code == 400
    assert response.json["error"] == "This file is not a readable audio file."


def test_animated_image_comes_back_exactly(client):
    frames = [Image.fromarray(np.full((8, 8), value, dtype=np.uint8)) for value in (0, 255)]
    buffer = io.BytesIO()
    frames[0].save(buffer, format="GIF", save_all=True, append_images=frames[1:])
    encoded = upload(client, "/api/encode", buffer.getvalue(), "moving.gif")
    assert encoded.status_code == 200
    assert upload(client, "/api/decode", encoded.data, "a.flac").data == buffer.getvalue()


def test_missing_file_is_reported(client):
    response = client.post("/api/encode", data={}, content_type="multipart/form-data")
    assert response.status_code == 400
    assert response.json["error"] == "Choose a file to convert."


def test_oversized_upload_is_refused(client, monkeypatch):
    monkeypatch.setitem(web.app.config, "MAX_CONTENT_LENGTH", 1000)
    response = upload(client, "/api/encode", b"x" * 5000)
    assert response.status_code == 413
    assert "MB" in response.json["error"]


def test_too_many_pixels_is_carried_as_a_file(client, monkeypatch):
    monkeypatch.setattr(web, "MAX_PIXELS", 100)
    response = upload(client, "/api/encode", png_bytes())
    assert response.status_code == 200
    assert json.loads(response.headers["X-Filetape-Result"])["method"] == "data"


def test_unknown_path_returns_json(client):
    response = client.get("/nope")
    assert response.status_code == 404
    assert "error" in response.json


def smooth_png(step=1):
    ramp = np.arange(256 * 256 * 3, dtype=np.uint32).reshape(256, 256, 3) * step // 9
    return png_bytes((ramp % 256).astype(np.uint8), compress_level=0)


def zip_bytes(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def batch(client, files):
    return upload(client, "/api/batch", zip_bytes(files), "folder.zip")


def unpack(response):
    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def result_of(response):
    return json.loads(response.headers["X-Filetape-Result"])


def test_batch_round_trip_restores_folder(client):
    first, second = smooth_png(1), smooth_png(2)
    files = {"trip/a.png": first, "trip/days/b.png": second, "notes.txt": b"keep me"}
    packed = batch(client, files)
    assert packed.status_code == 200
    assert packed.mimetype == "application/zip"
    result = result_of(packed)
    assert (result["compressed"], result["copied"], result["problems"]) == (2, 1, [])
    assert result["after"] == len(packed.data)
    contents = unpack(packed)
    assert set(contents) == {"trip/a.png.flac", "trip/days/b.png.flac", "notes.txt"}

    restored = upload(client, "/api/batch", packed.data, "folder.filetape.zip")
    assert result_of(restored)["restored"] == 2
    images = unpack(restored)
    assert set(images) == set(files)
    assert images["notes.txt"] == b"keep me"
    for name in ("trip/a.png", "trip/days/b.png"):
        expected = np.asarray(Image.open(io.BytesIO(files[name])))
        assert np.array_equal(np.asarray(Image.open(io.BytesIO(images[name]))), expected)


def test_batch_restores_a_jpeg_byte_for_byte(client):
    array = np.random.default_rng(2).integers(0, 255, (64, 64, 3), dtype=np.uint8)
    buffer = io.BytesIO()
    Image.fromarray(array).save(buffer, format="JPEG")
    packed = batch(client, {"photo.jpeg": buffer.getvalue()})
    restored = upload(client, "/api/batch", packed.data, "folder.filetape.zip")
    assert unpack(restored) == {"photo.jpeg": buffer.getvalue()}


def test_batch_keeps_an_image_it_cannot_shrink(client):
    source = png_bytes(np.random.default_rng(0).integers(0, 255, (32, 32, 3), dtype=np.uint8))
    response = batch(client, {"noise.png": source})
    assert result_of(response)["copied"] == 1
    assert unpack(response) == {"noise.png": source}


def test_batch_carries_any_file_and_brings_it_back(client):
    files = {"docs/notes.txt": b"plain text " * 400, "bad.png": b"not really a png" * 40, "good.png": smooth_png()}
    packed = batch(client, files)
    assert result_of(packed)["compressed"] == 3
    assert "docs/notes.txt.flac" in unpack(packed)
    restored = upload(client, "/api/batch", packed.data, "folder.filetape.zip")
    contents = unpack(restored)
    assert contents["docs/notes.txt"] == files["docs/notes.txt"]
    assert contents["bad.png"] == files["bad.png"]
    assert np.array_equal(np.asarray(Image.open(io.BytesIO(contents["good.png"]))), np.asarray(Image.open(io.BytesIO(files["good.png"]))))


def test_batch_copies_ordinary_music(client):
    response = batch(client, {"song.flac": b"not from pixel"})
    assert result_of(response)["problems"] == ["song.flac"]
    assert unpack(response) == {"song.flac": b"not from pixel"}


def test_batch_names_do_not_collide(client):
    response = batch(client, {"a.png": png_bytes(), "a.png.flac": b"plain", "A.PNG.FLAC": b"plain"})
    assert len({name.casefold() for name in unpack(response)}) == 3


@pytest.mark.parametrize("name", ["../evil.png", "/evil.png", "..\\evil.png", "C:/evil.png", "a:b.png"])
def test_batch_skips_paths_that_escape_the_folder(client, name):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        entry = zipfile.ZipInfo("placeholder")
        entry.filename = name
        archive.writestr(entry, png_bytes())
        archive.writestr("safe.txt", b"ok")
    response = upload(client, "/api/batch", buffer.getvalue(), "folder.zip")
    assert result_of(response)["skipped"] == 1
    assert set(unpack(response)) == {"safe.txt"}


def test_batch_ignores_folders_and_mac_metadata(client):
    response = batch(client, {"empty/": b"", "__MACOSX/._a.png": b"x", "a.txt": b"ok"})
    assert set(unpack(response)) == {"a.txt"}
    assert result_of(response)["problems"] == []


def test_batch_skips_a_damaged_entry(client):
    archive = bytearray(zip_bytes({"a.txt": b"hello hello", "b.txt": b"fine"}))
    archive[archive.index(b"hello hello")] ^= 0xFF
    response = upload(client, "/api/batch", bytes(archive), "folder.zip")
    assert result_of(response)["problems"] == ["a.txt"]
    assert set(unpack(response)) == {"b.txt"}


def test_batch_refuses_a_file_that_is_not_a_zip(client):
    response = upload(client, "/api/batch", b"plain text", "folder.zip")
    assert response.status_code == 400
    assert response.json["error"] == "This file is not a readable zip."


def test_batch_refuses_an_empty_zip(client):
    response = batch(client, {})
    assert response.status_code == 400
    assert "no files" in response.json["error"]


def test_batch_refuses_too_many_files(client, monkeypatch):
    monkeypatch.setattr(web, "MAX_BATCH_FILES", 1)
    response = batch(client, {"a.txt": b"1", "b.txt": b"2"})
    assert response.status_code == 400
    assert "up to 1 files" in response.json["error"]


def test_batch_refuses_a_zip_bomb(client, monkeypatch):
    monkeypatch.setattr(web, "MAX_BATCH_UNPACKED", 1000)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("bomb.txt", b"\\0" * 100_000)
    assert len(buffer.getvalue()) < 1000
    response = upload(client, "/api/batch", buffer.getvalue(), "folder.zip")
    assert response.status_code == 400
    assert "2 GB" in response.json["error"]


def test_batch_stops_when_the_output_grows_too_large(client, monkeypatch):
    monkeypatch.setattr(web, "MAX_BATCH_OUTPUT", 100)
    response = batch(client, {"a.bin": bytes(range(256)), "b.bin": b"x"})
    assert response.status_code == 400
    assert "2 GB" in response.json["error"]


def test_batch_runs_one_zip_at_a_time(client):
    assert web.batch_slot.acquire(blocking=False)
    try:
        response = batch(client, {"a.txt": b"1"})
    finally:
        web.batch_slot.release()
    assert response.status_code == 503
    assert batch(client, {"a.txt": b"1"}).status_code == 200


def test_batch_is_not_held_to_the_single_file_limit(client, monkeypatch):
    monkeypatch.setitem(web.app.config, "MAX_CONTENT_LENGTH", 1000)
    assert batch(client, {"a.bin": b"x" * 5000}).status_code == 200


def test_oversized_batch_is_refused_and_frees_the_slot(client, monkeypatch):
    monkeypatch.setattr(web, "MAX_BATCH_MB", 0)
    response = batch(client, {"a.txt": b"1"})
    assert response.status_code == 413
    assert "0 MB" in response.json["error"]
    assert web.batch_slot.acquire(blocking=False)
    web.batch_slot.release()
