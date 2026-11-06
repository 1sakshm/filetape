"""Store any file as FLAC audio and restore it exactly, with extra compression for images."""

import argparse
import dataclasses
import io
import lzma
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

import numpy as np
import pillow_jxl
import soundfile as sf
from PIL import Image

SAMPLE_RATE = 44100
MAGIC = b"PXL1"
HEADER = struct.Struct("<4sBIII")  # magic, mode code, width, height, CRC-32
ICC_LENGTH = struct.Struct("<I")  # follows the header when FLAG_ICC is set
FLAG_ICC = 0x80
MAX_ICC_SIZE = 16 * 1024 * 1024
MODES = ("L", "LA", "RGB", "RGBA")
FORMATS = ("PNG", "JPEG", "WEBP", "BMP", "TIFF", "GIF")

PAYLOAD_MAGIC = b"PXF1"
PAYLOAD_HEADER = struct.Struct("<4sBIII")  # magic, kind, payload size, ICC size, CRC-32
KIND_JPEG = 1  # JPEG XL that rebuilds the original JPEG file
KIND_PIXELS = 2  # lossless JPEG XL of the pixels
KIND_FILE = 3  # the original file, unchanged
KIND_JPEG_SPLIT = 4  # as KIND_JPEG, with the profile segments kept in the metadata
KIND_TRUEPIX = 5  # the Truepix codec's own file
KIND_DATA = 6  # any other file, with its name

TRUEPIX_MAGIC = b"TPX1"
TRUEPIX_HEADER = struct.Struct("<4sIIBBI")  # magic, width, height, channels, tiles per side, CRC-32
TRUEPIX_MAX_SIDE = 2  # all this tool writes; larger splits can demand gigabytes when decoded

JXL_EFFORT = 9
JXL_MAX_EFFORT = 10
JPEG_SEGMENT = struct.Struct("<II")  # offset in the original file, length

DATA_HEADER = struct.Struct("<BQH")  # method, original size, name length
DATA_RAW = 0
DATA_LZMA = 1
MAX_NAME_BYTES = 1024
SUFFIXES = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp", "BMP": ".bmp", "TIFF": ".tif", "GIF": ".gif"}
ALLOWED_SUFFIXES = {
    ".png": (".png",),
    ".jpg": (".jpg", ".jpeg"),
    ".webp": (".webp",),
    ".bmp": (".bmp",),
    ".tif": (".tif", ".tiff"),
    ".gif": (".gif",),
}


class PhotophoneError(Exception):
    """A problem with the input that the user can act on."""


@dataclasses.dataclass
class Restored:
    """Either the original file's bytes, or pixels to save as a PNG."""

    file: bytes | None = None
    name: str | None = None  # set for any file that isn't an image
    pixels: np.ndarray | None = None
    mode: str | None = None
    icc_profile: bytes | None = None


def forward_color(rgb):
    """Reversible RGB to YCoCg-R, so the channels share less information."""
    r, g, b = (rgb[..., i].astype(np.int32) for i in range(3))
    co = r - b
    t = b + (co >> 1)
    cg = g - t
    y = t + (cg >> 1)
    return np.stack([y, co, cg], axis=-1)


def inverse_color(ycocg):
    y, co, cg = (ycocg[..., i] for i in range(3))
    t = y - (cg >> 1)
    g = cg + t
    b = t - (co >> 1)
    r = b + co
    return np.stack([r, g, b], axis=-1)


def med_predict(left, up, up_left):
    """LOCO-I median edge detector, the predictor used by JPEG-LS."""
    low = np.minimum(left, up)
    high = np.maximum(left, up)
    return np.where(up_left >= high, low, np.where(up_left <= low, high, left + up - up_left))


def predict_forward(plane):
    """Replace each value with its difference from the predicted value."""
    padded = np.pad(plane, ((1, 0), (1, 0)))
    left, up, up_left = padded[1:, :-1].copy(), padded[:-1, 1:], padded[:-1, :-1].copy()
    # The first column has no left neighbour, so it predicts from the value above.
    left[:, 0] = up[:, 0]
    up_left[:, 0] = up[:, 0]
    return plane - med_predict(left, up, up_left)


def predict_inverse(residuals):
    """Undo predict_forward one diagonal at a time.

    Pixels on a diagonal depend only on the previous diagonal, so each one can
    be restored in a single NumPy step rather than a per-pixel Python loop.
    """
    height, width = residuals.shape
    padded = np.zeros((height + 1, width + 1), dtype=np.int32)
    for diagonal in range(height + width - 1):
        ys = np.arange(max(0, diagonal - width + 1), min(diagonal, height - 1) + 1)
        xs = diagonal - ys
        left, up, up_left = padded[ys + 1, xs], padded[ys, xs + 1], padded[ys, xs]
        if diagonal < height:  # The last entry is in the first column.
            left[-1] = up_left[-1] = up[-1]
        padded[ys + 1, xs + 1] = residuals[ys, xs] + med_predict(left, up, up_left)
    return padded[1:, 1:]


def pixels_to_residuals(pixels, mode):
    planes = pixels.astype(np.int32)
    if mode.startswith("RGB"):
        planes = np.concatenate([forward_color(pixels[..., :3]), planes[..., 3:]], axis=-1)
    channels = pixels.shape[-1]
    return np.stack([predict_forward(planes[..., i]) for i in range(channels)], axis=-1)


def residuals_to_pixels(residuals, mode):
    channels = residuals.shape[-1]
    planes = np.stack([predict_inverse(residuals[..., i]) for i in range(channels)], axis=-1)
    if mode.startswith("RGB"):
        planes = np.concatenate([inverse_color(planes[..., :3]), planes[..., 3:]], axis=-1)
    return planes


def write_audio(path, pixels, mode, icc_profile=None):
    height, width, channels = pixels.shape
    code = MODES.index(mode)
    crc = zlib.crc32(pixels.tobytes())
    extra = b""
    if icc_profile:
        code |= FLAG_ICC
        crc = zlib.crc32(icc_profile, crc)
        packed = zlib.compress(icc_profile, 9)
        extra = ICC_LENGTH.pack(len(packed)) + packed
    metadata = HEADER.pack(MAGIC, code, width, height, crc) + extra
    # Metadata lives in the samples, so it survives lossless audio conversion.
    prefix = np.zeros(math.ceil(len(metadata) / channels) * channels, dtype=np.int16)
    prefix[: len(metadata)] = np.frombuffer(metadata, dtype=np.uint8)
    samples = pixels_to_residuals(pixels, mode).reshape(-1, channels).astype(np.int16)
    samples = np.concatenate([prefix.reshape(-1, channels), samples])
    sf.write(path, samples, SAMPLE_RATE, format="FLAC", subtype="PCM_16", compression_level=1.0)


def stored_bytes(stream, start, stop, path):
    """Read metadata bytes from the start of the interleaved samples."""
    values = stream[start:stop]
    if values.size < stop - start or values.min(initial=0) < 0 or values.max(initial=0) > 255:
        raise PhotophoneError(f"{path} was not made by this tool")
    return values.astype(np.uint8).tobytes()


def unpack_icc(packed, path):
    inflater = zlib.decompressobj()
    try:
        icc_profile = inflater.decompress(packed, MAX_ICC_SIZE)
    except zlib.error as error:
        raise PhotophoneError(f"{path} is corrupted: bad colour profile") from error
    if inflater.unconsumed_tail or not inflater.eof:
        raise PhotophoneError(f"{path} is corrupted: bad colour profile")
    return icc_profile


def safe_name(name):
    """Keep only a plain file name, because a name read from audio can't be trusted."""
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch.isprintable() and ch not in '<>:"|?*').strip(" .")
    return name or "file"


def pack_data(original, name):
    """Compress a file if that helps, and keep its name alongside it."""
    title = safe_name(name).encode("utf-8", "ignore")[:MAX_NAME_BYTES]
    squeezed = lzma.compress(original, preset=6)
    method, body = (DATA_LZMA, squeezed) if len(squeezed) < len(original) else (DATA_RAW, original)
    return DATA_HEADER.pack(method, len(original), len(title)) + title + body


def unpack_data(payload, path, limit=None):
    """Return (name, bytes), checking what the file would demand before expanding it."""
    if len(payload) < DATA_HEADER.size:
        raise PhotophoneError(f"{path} is corrupted: checksum mismatch")
    method, size, length = DATA_HEADER.unpack_from(payload)
    if limit and size > limit:
        raise PhotophoneError(f"{path} would expand to more than the limit of {limit // 1024 ** 2} MB")
    start = DATA_HEADER.size + length
    title, body = payload[DATA_HEADER.size:start], payload[start:]
    if method == DATA_RAW:
        data = body
    elif method == DATA_LZMA:
        try:
            inflater = lzma.LZMADecompressor()
            data = inflater.decompress(body, max_length=size + 1)
        except lzma.LZMAError as error:
            raise PhotophoneError(f"{path} is corrupted: checksum mismatch") from error
    else:
        raise PhotophoneError(f"{path} is corrupted: checksum mismatch")
    if len(data) != size:
        raise PhotophoneError(f"{path} is corrupted: checksum mismatch")
    return safe_name(title.decode("utf-8", "replace")), data


def write_payload(path, kind, payload, crc, icc_profile=None):
    """Store bytes as mono samples, after a header kept readable as small values."""
    packed = zlib.compress(icc_profile, 9) if icc_profile else b""
    metadata = PAYLOAD_HEADER.pack(PAYLOAD_MAGIC, kind, len(payload), len(packed), crc) + packed
    padded = payload + b"\0" * (len(payload) % 2)
    samples = np.concatenate([
        np.frombuffer(metadata, dtype=np.uint8).astype(np.int16),
        np.frombuffer(padded, dtype="<i2"),
    ])
    sf.write(path, samples, SAMPLE_RATE, format="FLAC", subtype="PCM_16", compression_level=1.0)


def truepix_binary():
    """Find the optional Truepix codec: TRUEPIX_CORE, then PATH, then a Truepix folder alongside."""
    name = "truepix-core.exe" if os.name == "nt" else "truepix-core"
    beside = Path(__file__).resolve().parent.parent / "Truepix" / name
    found = os.environ.get("TRUEPIX_CORE") or shutil.which(name) or beside
    binary = Path(found)
    if not binary.is_file():
        raise PhotophoneError("the Truepix codec was not found; see README.md to install it")
    return binary


def run_truepix(*arguments):
    try:
        done = subprocess.run([str(truepix_binary()), *map(str, arguments)], capture_output=True, text=True)
    except OSError as error:
        raise PhotophoneError(f"cannot run the Truepix codec: {error}") from error
    if done.returncode:
        raise PhotophoneError(f"the Truepix codec failed: {done.stderr.strip()}")


def truepix_compress(pixels):
    height, width, channels = pixels.shape
    header = f"P7\nWIDTH {width}\nHEIGHT {height}\nDEPTH {channels}\nMAXVAL 255\nTUPLTYPE X\nENDHDR\n"
    with tempfile.TemporaryDirectory() as folder:
        raw, packed = Path(folder) / "in.pam", Path(folder) / "out.tpix"
        raw.write_bytes(header.encode() + pixels.tobytes())
        run_truepix("c", raw, packed)
        return packed.read_bytes()


def truepix_expand(payload, path, max_pixels=None):
    """Decode a Truepix file after checking what it would demand of the machine."""
    if len(payload) < TRUEPIX_HEADER.size:
        raise PhotophoneError(f"{path} is corrupted: checksum mismatch")
    magic, width, height, channels, side, _ = TRUEPIX_HEADER.unpack_from(payload)
    if magic != TRUEPIX_MAGIC or not 1 <= channels <= len(MODES) or not 1 <= side <= TRUEPIX_MAX_SIDE:
        raise PhotophoneError(f"{path} is corrupted: checksum mismatch")
    if max_pixels and width * height > max_pixels:
        raise PhotophoneError(f"{path} is too large; the limit is {max_pixels:,} pixels")
    size = width * height * channels
    with tempfile.TemporaryDirectory() as folder:
        packed, raw = Path(folder) / "in.tpix", Path(folder) / "out.pnm"
        packed.write_bytes(payload)
        run_truepix("d", packed, raw)
        data = raw.read_bytes()[-size:]
    return np.frombuffer(data, dtype=np.uint8).reshape(height, width, channels), MODES[channels - 1]


def read_payload(samples, path, max_pixels=None):
    stream = samples.reshape(-1)
    metadata = stored_bytes(stream, 0, PAYLOAD_HEADER.size, path)
    _, kind, size, icc_size, crc = PAYLOAD_HEADER.unpack(metadata)
    start = PAYLOAD_HEADER.size + icc_size
    icc_profile = unpack_icc(stored_bytes(stream, PAYLOAD_HEADER.size, start, path), path) if icc_size else None
    body = stream[start:]
    if body.size != math.ceil(size / 2):
        raise PhotophoneError(f"{path} is truncated or corrupted")
    payload = body.astype("<i2").tobytes()[:size]
    try:
        if kind == KIND_FILE:
            restored = Restored(file=payload)
            actual = zlib.crc32(payload)
        elif kind == KIND_DATA:
            name, data = unpack_data(payload, path, max_pixels and max_pixels * 4)
            restored = Restored(file=data, name=name)
            actual = zlib.crc32(data)
        elif kind in (KIND_JPEG, KIND_JPEG_SPLIT):
            is_jpeg, _, data, _, _ = pillow_jxl.Decoder()(payload)
            if not is_jpeg:
                raise PhotophoneError(f"{path} is corrupted: checksum mismatch")
            data = bytes(data)
            restored = Restored(file=join_jpeg_segments(data, icc_profile) if icc_profile else data)
            actual = zlib.crc32(restored.file)
        elif kind == KIND_PIXELS:
            _, info, data, _, _ = pillow_jxl.Decoder()(payload)
            shape = (info.height, info.width, len(info.mode))
            pixels = np.frombuffer(bytes(data), dtype=np.uint8).reshape(shape)
            restored = Restored(pixels=pixels, mode=info.mode, icc_profile=icc_profile)
            actual = content_crc(pixels, icc_profile)
        elif kind == KIND_TRUEPIX:
            pixels, mode = truepix_expand(payload, path, max_pixels)
            restored = Restored(pixels=pixels, mode=mode, icc_profile=icc_profile)
            actual = content_crc(pixels, icc_profile)
        else:
            raise PhotophoneError(f"{path} was not made by this tool")
    except (RuntimeError, ValueError) as error:
        raise PhotophoneError(f"{path} is corrupted: checksum mismatch") from error
    if actual != crc:
        raise PhotophoneError(f"{path} is corrupted: checksum mismatch")
    return restored


def restore(path, max_pixels=None):
    """Read either kind of stream and return a checksum-verified Restored."""
    samples = read_samples(path, max_pixels)
    if stored_bytes(samples.reshape(-1), 0, len(MAGIC), path) == PAYLOAD_MAGIC:
        return read_payload(samples, path, max_pixels)
    pixels, mode, icc_profile = read_pixel_stream(samples, path, max_pixels)
    return Restored(pixels=pixels, mode=mode, icc_profile=icc_profile)


def read_samples(path, max_pixels=None):
    try:
        if max_pixels and sf.info(path).frames > max_pixels + MAX_ICC_SIZE:
            raise PhotophoneError(f"{path} is too large; the limit is {max_pixels:,} pixels")
        samples, _ = sf.read(path, dtype="int16", always_2d=True)
    except sf.LibsndfileError as error:
        raise PhotophoneError(f"{path} is not a readable audio file") from error
    return samples


def read_audio(path, max_pixels=None):
    """Return (pixels, mode, icc_profile) from a pixel stream, verifying the checksum."""
    return read_pixel_stream(read_samples(path, max_pixels), path, max_pixels)


def read_pixel_stream(samples, path, max_pixels=None):
    channels = samples.shape[1]
    stream = samples.reshape(-1)
    magic, code, width, height, crc = HEADER.unpack(stored_bytes(stream, 0, HEADER.size, path))
    has_icc = bool(code & FLAG_ICC)
    code &= ~FLAG_ICC
    if magic != MAGIC or code >= len(MODES) or len(MODES[code]) != channels:
        raise PhotophoneError(f"{path} was not made by this tool")
    if max_pixels and width * height > max_pixels:
        raise PhotophoneError(f"{path} is too large; the limit is {max_pixels:,} pixels")
    metadata_size = HEADER.size
    icc_profile = None
    if has_icc:
        start = HEADER.size + ICC_LENGTH.size
        packed_size = ICC_LENGTH.unpack(stored_bytes(stream, HEADER.size, start, path))[0]
        metadata_size = start + packed_size
        icc_profile = unpack_icc(stored_bytes(stream, start, metadata_size, path), path)
    frames = math.ceil(metadata_size / channels)
    if width * height == 0 or len(samples) - frames != width * height:
        raise PhotophoneError(f"{path} is truncated or corrupted")
    mode = MODES[code]
    residuals = samples[frames:].astype(np.int32).reshape(height, width, channels)
    pixels = residuals_to_pixels(residuals, mode)
    if pixels.min() < 0 or pixels.max() > 255:
        raise PhotophoneError(f"{path} is corrupted: checksum mismatch")
    pixels = pixels.astype(np.uint8)
    actual = zlib.crc32(pixels.tobytes())
    if icc_profile:
        actual = zlib.crc32(icc_profile, actual)
    if actual != crc:
        raise PhotophoneError(f"{path} is corrupted: checksum mismatch")
    return pixels, mode, icc_profile


def load_image(path, max_pixels=None):
    """Return (pixels, mode, icc_profile), refusing what can't be kept exactly."""
    try:
        with Image.open(path, formats=FORMATS) as image:
            image_format = image.format
            if max_pixels and image.width * image.height > max_pixels:
                raise PhotophoneError(f"{path} is too large; the limit is {max_pixels:,} pixels")
            if getattr(image, "n_frames", 1) > 1:
                raise PhotophoneError(f"{path} is animated; only single images are supported")
            # Pillow reduces 16-bit RGB to 8 bits without complaint, so check the raw layout.
            raw_layout = str(image.tile[0][3]) if image.tile else ""
            if ";16" in raw_layout or image.mode.startswith("I") or image.mode == "F":
                raise PhotophoneError(f"{path} uses 16-bit or higher depth; only 8-bit is supported")
            icc_profile = image.info.get("icc_profile")
            if "transparency" in image.info or image.mode == "PA":
                image = image.convert("RGBA")
            elif image.mode == "P":
                image = image.convert("RGB")
            elif image.mode == "1":
                image = image.convert("L")
            if image.mode not in MODES:
                raise PhotophoneError(f"{path} uses unsupported colour mode {image.mode}")
            pixels = np.asarray(image, dtype=np.uint8)
    except Image.UnidentifiedImageError as error:
        raise PhotophoneError(f"{path} is not a supported image") from error
    except Image.DecompressionBombError as error:
        raise PhotophoneError(f"{path} is too large") from error
    except (OSError, ValueError) as error:
        raise PhotophoneError(f"cannot read {path}: {error}") from error
    if pixels.ndim == 2:
        pixels = pixels[..., np.newaxis]
    return pixels, image.mode, icc_profile, image_format


def temp_path_for(output):
    """Create a temporary file beside the output, so the final move is atomic."""
    try:
        handle, name = tempfile.mkstemp(suffix=output.suffix, dir=output.parent)
    except OSError as error:
        raise PhotophoneError(f"cannot write to {output.parent}: {error}") from error
    os.close(handle)
    return name


def check_output(output, force):
    if output.exists() and not force:
        raise PhotophoneError(f"{output} already exists; pass --force to overwrite it")


def write_stored(path, data):
    write_payload(path, KIND_FILE, data, zlib.crc32(data))


def split_jpeg_segments(data):
    """Return the JPEG without its ICC segments, and those segments packed for storage."""
    kept, packed, position = [data[:2]], [], 2
    while position + 4 <= len(data) and data[position] == 0xFF and data[position + 1] != 0xDA:
        end = position + 2 + int.from_bytes(data[position + 2 : position + 4], "big")
        segment = data[position:end]
        if data[position + 1] == 0xE2 and segment[4:16] == b"ICC_PROFILE\0":
            packed.append(JPEG_SEGMENT.pack(position, len(segment)) + segment)
        else:
            kept.append(segment)
        position = end
    kept.append(data[position:])
    return b"".join(kept), b"".join(packed)


def join_jpeg_segments(data, packed):
    """Undo split_jpeg_segments. Offsets ascend, so each insert lands in its final place."""
    position = 0
    while position < len(packed):
        offset, length = JPEG_SEGMENT.unpack_from(packed, position)
        position += JPEG_SEGMENT.size
        data = data[:offset] + packed[position : position + length] + data[offset:]
        position += length
    return data


def write_jpeg(path, data, width, height, mode, effort=JXL_EFFORT):
    """Convert the JPEG's own data to JPEG XL, which can rebuild the file exactly."""
    encoder = pillow_jxl.Encoder(
        mode=mode, lossless=True, quality=100, use_container=True, effort=effort
    )
    try:
        payload = encoder(data, width, height, jpeg_encode=True)
    except RuntimeError:
        # JPEG XL refuses some colour profiles, so convert the rest and keep them aside.
        stripped, packed = split_jpeg_segments(data)
        if not packed:
            raise
        payload = encoder(stripped, width, height, jpeg_encode=True)
        write_payload(path, KIND_JPEG_SPLIT, payload, zlib.crc32(data), packed)
        return
    write_payload(path, KIND_JPEG, payload, zlib.crc32(data))


def write_jxl_pixels(path, pixels, mode, icc_profile, effort=JXL_EFFORT):
    height, width, _ = pixels.shape
    encoder = pillow_jxl.Encoder(
        mode=mode, lossless=True, quality=100, use_original_profile=True, effort=effort
    )
    payload = encoder(pixels.tobytes(), width, height, jpeg_encode=False)
    write_payload(path, KIND_PIXELS, payload, content_crc(pixels, icc_profile), icc_profile)


def write_truepix(path, pixels, icc_profile):
    write_payload(path, KIND_TRUEPIX, truepix_compress(pixels), content_crc(pixels, icc_profile), icc_profile)


def content_crc(pixels, icc_profile):
    crc = zlib.crc32(pixels.tobytes())
    return zlib.crc32(icc_profile, crc) if icc_profile else crc


def matches_source(restored, original, pixels, icc_profile):
    if restored.file is not None:
        return restored.file == original
    return np.array_equal(restored.pixels, pixels) and restored.icc_profile == icc_profile


def encode(source, output, force=False, max_pixels=None, effort=JXL_EFFORT, name=None):
    """Try each method, keep the smallest verified file and return a summary."""
    check_output(output, force)
    original = source.read_bytes()
    try:
        pixels, mode, icc_profile, image_format = load_image(source, max_pixels)
        height, width, _ = pixels.shape
    except PhotophoneError:  # Not an image we can model, so it is carried as plain data.
        pixels = mode = icc_profile = None
        image_format = "data"
        height = width = 0
    data_name = safe_name(name or source.name)
    if pixels is None:
        writers = {"data": lambda path: write_payload(path, KIND_DATA, pack_data(original, data_name), zlib.crc32(original))}
    else:
        writers = {"stored": lambda path: write_stored(path, original)}
    if image_format == "JPEG":
        writers["jpeg"] = lambda path: write_jpeg(path, original, width, height, mode, effort)
    elif pixels is not None:
        writers["jxl"] = lambda path: write_jxl_pixels(path, pixels, mode, icc_profile, effort)
        writers["residual"] = lambda path: write_audio(path, pixels, mode, icc_profile)
        if effort >= JXL_MAX_EFFORT:  # Much slower, so only when the smallest file is wanted.
            writers["truepix"] = lambda path: write_truepix(path, pixels, icc_profile)

    best, best_method = None, None
    try:
        for method, write in writers.items():
            temp = temp_path_for(output)
            try:
                write(temp)
                restored = restore(temp)
            except (RuntimeError, PhotophoneError):  # Some files can't use every method.
                os.remove(temp)
                continue
            smaller = best is None or os.path.getsize(temp) < os.path.getsize(best)
            if matches_source(restored, original, pixels, icc_profile) and smaller:
                if best:
                    os.remove(best)
                best, best_method = temp, method
            else:
                os.remove(temp)
        if best is None:
            raise PhotophoneError("internal error: no method restored the image; nothing was written")
        frames = sf.info(best).frames
        os.replace(best, output)
    finally:
        if best and os.path.exists(best):
            os.remove(best)
    return {
        "before": len(original),
        "after": output.stat().st_size,
        "width": width,
        "height": height,
        "mode": mode,
        "method": best_method,
        "seconds": frames / SAMPLE_RATE,
        "icc_profile": bool(icc_profile),
        "name": data_name if pixels is None else None,
    }


def file_details(data):
    """Return the suffix and open image for restored file contents."""
    image = Image.open(io.BytesIO(data), formats=FORMATS)
    return SUFFIXES[image.format], image


def decode(source, output=None, force=False, max_pixels=None):
    """Restore the original and return a summary. Without an output, name it after the source."""
    restored = restore(source, max_pixels)
    if restored.name is not None:
        target = Path(restored.name)
        output = output or source.with_name(f"{target.stem}.decoded{target.suffix}")
        check_output(output, force)
        temp = temp_path_for(output)
        try:
            Path(temp).write_bytes(restored.file)
            os.replace(temp, output)
        finally:
            if os.path.exists(temp):
                os.remove(temp)
        return {"path": output, "name": restored.name, "size": len(restored.file), "suffix": target.suffix}
    if restored.file is not None:
        suffix, image = file_details(restored.file)
        width, height = image.size
        mode, has_profile = image.mode, "icc_profile" in image.info
    else:
        suffix = ".png"
        height, width, _ = restored.pixels.shape
        mode, has_profile = restored.mode, bool(restored.icc_profile)
    output = output or source.with_suffix(f".decoded{suffix}")
    if output.suffix.lower() not in ALLOWED_SUFFIXES[suffix]:
        raise PhotophoneError(f"this audio holds a {suffix} image, so the output must be a {suffix} file")
    check_output(output, force)
    temp = temp_path_for(output)
    try:
        if restored.file is not None:
            Path(temp).write_bytes(restored.file)
        else:
            channels = restored.pixels.shape[-1]
            shaped = restored.pixels[..., 0] if channels == 1 else restored.pixels
            Image.fromarray(shaped).save(
                temp, format="PNG", optimize=True, icc_profile=restored.icc_profile
            )
        os.replace(temp, output)
    finally:
        if os.path.exists(temp):
            os.remove(temp)
    return {
        "path": output,
        "suffix": suffix,
        "width": width,
        "height": height,
        "mode": mode,
        "icc_profile": has_profile,
    }


def report_encode(source, output, result):
    before, after = result["before"], result["after"]
    print(f"{source} -> {output}")
    change = (after - before) / before if before else 0
    print(f"{before:,} -> {after:,} bytes ({change:+.1%}), {'bytes' if result['name'] else 'pixels'} verified")
    if after >= before:
        print("warning: the audio file is not smaller than the original", file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="photophone", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, summary in (
        ("encode", "any file to FLAC (default output: INPUT with a .flac suffix)"),
        ("decode", "FLAC back to the original file (default output: NAME.decoded.EXTENSION)"),
    ):
        command = commands.add_parser(name, help=summary)
        command.add_argument("input", type=Path)
        command.add_argument("-o", "--output", type=Path)
        command.add_argument("-f", "--force", action="store_true", help="overwrite the output")
        if name == "encode":
            command.add_argument("--max", action="store_true", help="smallest output, much slower")
    args = parser.parse_args(argv)

    try:
        if not args.input.is_file():
            raise PhotophoneError(f"{args.input} does not exist")
        if args.command == "encode":
            output = args.output or args.input.with_suffix(".flac")
            effort = JXL_MAX_EFFORT if args.max else JXL_EFFORT
            report_encode(args.input, output, encode(args.input, output, args.force, effort=effort))
        else:
            result = decode(args.input, args.output, args.force)
            print(f"{args.input} -> {result['path']}, checksum verified")
    except PhotophoneError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
