"""Web interface for Photophone: upload an image or audio file and download the result."""

import json
import mimetypes
import os
import shutil
import tempfile
import threading
import zipfile
import zlib
from pathlib import Path, PurePosixPath

from flask import Flask, Response, jsonify, request, send_file
from waitress import serve
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge

import photophone

MAX_UPLOAD_MB = 40
MAX_PIXELS = 50_000_000
MAX_BATCH_MB = 500
MAX_BATCH_FILES = 1000
MAX_BATCH_UNPACKED = 2 * 1024 * 1024 * 1024
MAX_BATCH_OUTPUT = 2 * 1024 * 1024 * 1024
MAX_LISTED_PROBLEMS = 20
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' blob:; "
    "media-src blob:; connect-src 'self'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'"
)

mimetypes.add_type("audio/flac", ".flac")

app = Flask(__name__, static_folder="static", static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
batch_slot = threading.Semaphore(1)  # Each image is held in memory, so run one zip at a time.


def error_response(message, status):
    return jsonify(error=message), status


def convert(action):
    """Run a conversion in a temporary folder that is removed afterwards."""
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return error_response("Choose a file to convert.", 400)
    with tempfile.TemporaryDirectory() as folder:
        source = Path(folder) / "upload"
        upload.save(source)
        try:
            output, result = action(source)
        except photophone.PhotophoneError as error:
            message = str(error).replace(str(source), "this file")
            return error_response(message[0].upper() + message[1:] + ".", 400)
        body = output.read_bytes()
    response = Response(body, mimetype=mimetypes.guess_type(output.name)[0])
    response.headers["X-Photophone-Result"] = json.dumps(result)
    return response


@app.get("/")
def home():
    return app.send_static_file("index.html")


@app.post("/api/encode")
def encode():
    def run(source):
        output = source.with_suffix(".flac")
        return output, photophone.encode(source, output, max_pixels=MAX_PIXELS)

    return convert(run)


@app.post("/api/decode")
def decode():
    def run(source):
        result = photophone.decode(source, max_pixels=MAX_PIXELS)
        return result.pop("path"), result

    return convert(run)


def safe_entry_name(name):
    """Return a zip entry's path, or None if unpacking it could escape the folder."""
    name = name.replace("\\", "/")
    parts = name.split("/")
    if name.startswith("/") or ".." in parts or ":" in name:
        return None
    return "/".join(part for part in parts if part not in ("", "."))


def unique_name(name, used):
    """Keep two entries from landing on one path, even on a case-insensitive disk."""
    path = PurePosixPath(name)
    candidate, number = name, 1
    while candidate.casefold() in used:
        number += 1
        candidate = str(path.with_name(f"{path.stem} ({number}){path.suffix}"))
    used.add(candidate.casefold())
    return candidate


def compress_entry(source, name):
    """Return the audio and its name, or None if it would not be smaller than the image."""
    output = source.with_suffix(".flac")
    result = photophone.encode(source, output, max_pixels=MAX_PIXELS)
    if result["after"] >= result["before"]:
        return None
    return output, f"{name}.flac"


def restore_entry(source, name):
    """Return the rebuilt image and its name, undoing the name compress_entry gave it."""
    result = photophone.decode(source, max_pixels=MAX_PIXELS)
    path = PurePosixPath(name).with_suffix("")
    if path.suffix.lower() not in photophone.ALLOWED_SUFFIXES[result["suffix"]]:
        path = path.with_suffix(result["suffix"])
    return result["path"], str(path)


CONVERTERS = {suffix: ("compressed", compress_entry) for suffixes in photophone.ALLOWED_SUFFIXES.values() for suffix in suffixes}
CONVERTERS |= {suffix: ("restored", restore_entry) for suffix in (".flac", ".wav")}


def add_entry(output, path, name, date_time, compression):
    entry = zipfile.ZipInfo(name, date_time)
    entry.compress_type = compression
    with path.open("rb") as source, output.open(entry, "w", force_zip64=True) as target:
        shutil.copyfileobj(source, target)


def convert_folder(archive, output, packed):
    """Convert each entry in turn, copying what can't be converted, and return a summary."""
    entries = [
        entry for entry in archive.infolist()
        if not entry.is_dir() and not entry.filename.startswith("__MACOSX/")
    ]
    if not entries:
        raise photophone.PhotophoneError("this zip has no files in it")
    if len(entries) > MAX_BATCH_FILES:
        raise photophone.PhotophoneError(f"a zip can hold up to {MAX_BATCH_FILES:,} files")
    if sum(entry.file_size for entry in entries) > MAX_BATCH_UNPACKED:
        raise photophone.PhotophoneError("this zip unpacks to more than the limit of 2 GB")

    summary = {"compressed": 0, "restored": 0, "copied": 0, "skipped": 0, "problems": []}
    used = set()

    def note_problem(name):
        if len(summary["problems"]) < MAX_LISTED_PROBLEMS:
            summary["problems"].append(name)

    for entry in entries:
        name = safe_entry_name(entry.filename)
        if not name:
            summary["skipped"] += 1
            note_problem(entry.filename)
            continue
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "entry"
            try:
                with archive.open(entry) as stored, source.open("wb") as target:
                    shutil.copyfileobj(stored, target)
            except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError, zlib.error):
                summary["skipped"] += 1  # Damaged, encrypted or an unknown compression method.
                note_problem(name)
                continue

            key, convert = CONVERTERS.get(PurePosixPath(name).suffix.lower(), ("copied", None))
            converted = None
            try:
                converted = convert and convert(source, name)
            except (photophone.PhotophoneError, MemoryError):
                note_problem(name)
            if converted:
                path, new_name = converted
                add_entry(output, path, unique_name(new_name, used), entry.date_time, zipfile.ZIP_STORED)
                summary[key] += 1
            else:
                add_entry(output, source, unique_name(name, used), entry.date_time, zipfile.ZIP_DEFLATED)
                summary["copied"] += 1
        if packed.tell() > MAX_BATCH_OUTPUT:
            raise photophone.PhotophoneError("the converted zip would be over the limit of 2 GB")
    return summary


@app.post("/api/batch")
def batch():
    if not batch_slot.acquire(blocking=False):
        return error_response("Another zip is being converted. Try again when it finishes.", 503)
    packed = tempfile.TemporaryFile()
    try:
        request.max_content_length = MAX_BATCH_MB * 1024 * 1024
        upload = request.files.get("file")
        if upload is None or not upload.filename:
            raise photophone.PhotophoneError("choose a zip file to convert")
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "upload.zip"
            upload.save(source)
            try:
                with zipfile.ZipFile(source) as archive, zipfile.ZipFile(packed, "w") as output:
                    result = convert_folder(archive, output, packed)
            except zipfile.BadZipFile:
                raise photophone.PhotophoneError("this file is not a readable zip") from None
            result["before"] = source.stat().st_size
    except photophone.PhotophoneError as error:
        packed.close()
        message = str(error)
        return error_response(message[0].upper() + message[1:] + ".", 400)
    except BaseException:
        packed.close()
        raise
    finally:
        batch_slot.release()
    result["after"] = packed.seek(0, os.SEEK_END)
    packed.seek(0)
    response = send_file(packed, mimetype="application/zip")
    response.content_length = result["after"]
    response.headers["X-Photophone-Result"] = json.dumps(result)
    return response


@app.after_request
def add_security_headers(response):
    response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.errorhandler(RequestEntityTooLarge)
def upload_too_large(_):
    limit = MAX_BATCH_MB if request.path == "/api/batch" else MAX_UPLOAD_MB
    return error_response(f"Files can be up to {limit} MB.", 413)


@app.errorhandler(HTTPException)
def http_error(error):
    return error_response(error.description, error.code)


@app.errorhandler(Exception)
def unexpected_error(error):
    app.logger.exception("Conversion failed", exc_info=error)
    return error_response("Something went wrong while converting this file.", 500)


def main():
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    print(f"Photophone is running at http://{host}:{port}")
    serve(app, host=host, port=port)


if __name__ == "__main__":
    main()
