import functools
import struct
import sys
import zlib
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from PIL import Image, ImageCms

sys.path.insert(0, str(Path(__file__).parent.parent))
import photophone

RNG = np.random.default_rng(0)


def noise(*shape):
    return RNG.integers(0, 256, shape, dtype=np.uint8)


def gradient(height, width, channels):
    y, x = np.mgrid[:height, :width]
    return np.stack([(x + y * (i + 1)) % 256 for i in range(channels)], axis=-1).astype(np.uint8)


def round_trip(tmp_path, source):
    audio, restored = tmp_path / "a.flac", tmp_path / "b.png"
    assert photophone.main(["encode", str(source), "-o", str(audio)]) == 0
    assert photophone.main(["decode", str(audio), "-o", str(restored)]) == 0
    return Image.open(restored)


def write_16bit_png(path, color_type, channels):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    ihdr = struct.pack(">IIBBBBB", 4, 4, 16, color_type, 0, 0, 0)
    rows = b"".join(b"\0" + noise(8 * channels).tobytes() for _ in range(4))
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")
    )


@pytest.mark.parametrize("mode,channels", [("L", 1), ("LA", 2), ("RGB", 3), ("RGBA", 4)])
@pytest.mark.parametrize("size", [(1, 1), (1, 9), (9, 1), (2, 2), (37, 53)])
@pytest.mark.parametrize("make", [noise, gradient])
def test_round_trip_is_exact(tmp_path, mode, channels, size, make):
    array = make(*size, channels)
    source = tmp_path / "in.png"
    Image.fromarray(array.squeeze(-1) if channels == 1 else array).save(source)
    restored = round_trip(tmp_path, source)
    assert restored.mode == mode
    assert np.array_equal(np.asarray(restored).reshape(array.shape), array)


@pytest.mark.parametrize("value", [0, 255])
def test_extreme_alternating_values(tmp_path, value):
    array = np.full((16, 16, 3), value, dtype=np.uint8)
    array[::2, 1::2] = 255 - value
    array[1::2, ::2, 1] = 255 - value
    source = tmp_path / "in.png"
    Image.fromarray(array).save(source)
    assert np.array_equal(np.asarray(round_trip(tmp_path, source)), array)


def test_predictor_inverts_exactly():
    plane = noise(64, 91).astype(np.int32)
    assert np.array_equal(photophone.predict_inverse(photophone.predict_forward(plane)), plane)


def test_palette_with_transparency_keeps_rendered_pixels(tmp_path):
    image = Image.fromarray(noise(20, 20, 3)).quantize(16)
    source = tmp_path / "in.png"
    image.save(source, transparency=3)
    expected = np.asarray(Image.open(source).convert("RGBA"))
    assert np.array_equal(np.asarray(round_trip(tmp_path, source).convert("RGBA")), expected)


def test_rgb_colour_key_transparency_is_kept(tmp_path):
    array = noise(12, 12, 3)
    array[0, 0] = (1, 2, 3)
    source = tmp_path / "in.png"
    Image.fromarray(array).save(source, transparency=(1, 2, 3))
    restored = np.asarray(round_trip(tmp_path, source).convert("RGBA"))
    assert restored.shape[-1] == 4 and restored[0, 0, 3] == 0


def test_bilevel_image(tmp_path):
    source = tmp_path / "in.png"
    Image.fromarray(noise(10, 10) > 127).save(source)
    expected = np.asarray(Image.open(source).convert("L"))
    assert np.array_equal(np.asarray(round_trip(tmp_path, source).convert("L")), expected)


@pytest.mark.parametrize("color_type,channels", [(0, 1), (2, 3), (4, 2), (6, 4)])
def test_16_bit_images_are_carried_as_files(tmp_path, color_type, channels):
    source = tmp_path / "in.png"
    write_16bit_png(source, color_type, channels)
    carries_exactly(tmp_path, source)


def test_animated_image_is_carried_as_a_file(tmp_path):
    source = tmp_path / "in.gif"
    frames = [Image.fromarray(noise(8, 8)) for _ in range(2)]
    frames[0].save(source, save_all=True, append_images=frames[1:])
    carries_exactly(tmp_path, source)


def test_cmyk_is_carried_as_a_file(tmp_path):
    source = tmp_path / "in.jpg"
    Image.fromarray(noise(8, 8, 3)).convert("CMYK").save(source)
    carries_exactly(tmp_path, source)


def test_broken_image_is_carried_as_a_file(tmp_path):
    source = tmp_path / "in.png"
    source.write_bytes(b"not an image at all")
    carries_exactly(tmp_path, source)


def test_missing_output_folder_is_reported(tmp_path, capsys):
    source = tmp_path / "in.png"
    Image.fromarray(gradient(8, 8, 3)).save(source)
    assert photophone.main(["encode", str(source), "-o", str(tmp_path / "no" / "a.flac")]) == 1
    assert "cannot write" in capsys.readouterr().err


def test_corrupted_audio_is_detected(tmp_path, capsys):
    source, audio = tmp_path / "in.png", tmp_path / "a.flac"
    Image.fromarray(gradient(32, 32, 3)).save(source)
    photophone.main(["encode", str(source), "-o", str(audio)])
    samples, rate = sf.read(audio, dtype="int16", always_2d=True)
    samples[-3, 0] ^= 1
    sf.write(audio, samples, rate, format="FLAC", subtype="PCM_16")
    assert photophone.main(["decode", str(audio), "-o", str(tmp_path / "b.png")]) == 1
    assert "corrupted" in capsys.readouterr().err
    assert not (tmp_path / "b.png").exists()


def test_truncated_audio_is_detected(tmp_path, capsys):
    source, audio = tmp_path / "in.png", tmp_path / "a.flac"
    Image.fromarray(gradient(32, 32, 3)).save(source)
    photophone.main(["encode", str(source), "-o", str(audio)])
    samples, rate = sf.read(audio, dtype="int16", always_2d=True)
    sf.write(audio, samples[:-3], rate, format="FLAC", subtype="PCM_16")
    assert photophone.main(["decode", str(audio), "-o", str(tmp_path / "b.png")]) == 1
    assert "truncated" in capsys.readouterr().err


def test_foreign_audio_is_refused(tmp_path, capsys):
    audio = tmp_path / "song.flac"
    wave = (np.sin(np.arange(4410) / 10) * 20000).astype(np.int16)
    sf.write(audio, wave, 44100, format="FLAC", subtype="PCM_16")
    assert photophone.main(["decode", str(audio), "-o", str(tmp_path / "b.png")]) == 1
    assert "not made by this tool" in capsys.readouterr().err


def test_survives_lossless_conversion_to_wav(tmp_path):
    array = gradient(20, 30, 3)
    source, audio, wav = tmp_path / "in.png", tmp_path / "a.flac", tmp_path / "a.wav"
    Image.fromarray(array).save(source)
    photophone.main(["encode", str(source), "-o", str(audio)])
    samples, rate = sf.read(audio, dtype="int16", always_2d=True)
    sf.write(wav, samples, rate, subtype="PCM_16")
    assert photophone.main(["decode", str(wav), "-o", str(tmp_path / "b.png")]) == 0
    assert np.array_equal(np.asarray(Image.open(tmp_path / "b.png")), array)


def test_existing_output_is_not_overwritten(tmp_path):
    source, audio = tmp_path / "in.png", tmp_path / "a.flac"
    Image.fromarray(gradient(8, 8, 3)).save(source)
    audio.write_bytes(b"keep me")
    assert photophone.main(["encode", str(source), "-o", str(audio)]) == 1
    assert audio.read_bytes() == b"keep me"
    assert photophone.main(["encode", str(source), "-o", str(audio), "--force"]) == 0
    assert audio.read_bytes() != b"keep me"


def test_decode_refuses_lossy_output(tmp_path, capsys):
    source, audio = tmp_path / "in.png", tmp_path / "a.flac"
    Image.fromarray(gradient(8, 8, 3)).save(source)
    photophone.main(["encode", str(source), "-o", str(audio)])
    assert photophone.main(["decode", str(audio), "-o", str(tmp_path / "b.jpg")]) == 1
    assert ".png" in capsys.readouterr().err


def test_default_paths_do_not_collide_with_the_original(tmp_path):
    source = tmp_path / "photo.png"
    Image.fromarray(gradient(8, 8, 3)).save(source)
    original = source.read_bytes()
    assert photophone.main(["encode", str(source)]) == 0
    assert photophone.main(["decode", str(tmp_path / "photo.flac")]) == 0
    assert (tmp_path / "photo.decoded.png").exists()
    assert source.read_bytes() == original


def test_warns_when_output_is_larger(tmp_path, capsys):
    source = tmp_path / "in.png"
    Image.fromarray(noise(64, 64, 3)).save(source)
    photophone.main(["encode", str(source), "-o", str(tmp_path / "a.flac")])
    assert "not smaller" in capsys.readouterr().err


def smooth_photo(height=256, width=256):
    y, x = np.mgrid[:height, :width]
    array = np.stack([128 + 80 * np.sin(x / 19), 128 + 80 * np.cos(y / 23), 128 + 60 * np.sin((x + y) / 31)], axis=-1)
    return np.clip(array + RNG.normal(0, 3, array.shape), 0, 255).astype(np.uint8)


def test_jpeg_comes_back_byte_identical_and_smaller(tmp_path):
    source, audio = tmp_path / "in.jpg", tmp_path / "a.flac"
    Image.fromarray(smooth_photo()).save(source, quality=90, exif=b"Exif\0\0MM\0*\0\0\0\x08\0\0\0\0\0\0")
    result = photophone.encode(source, audio)
    assert result["method"] == "jpeg" and result["after"] < result["before"]
    decoded = photophone.decode(audio, tmp_path / "b.jpg")
    assert decoded["suffix"] == ".jpg"
    assert (tmp_path / "b.jpg").read_bytes() == source.read_bytes()


def test_jpeg_default_output_keeps_its_format(tmp_path):
    source = tmp_path / "photo.jpg"
    Image.fromarray(smooth_photo(64, 64)).save(source, quality=90)
    photophone.main(["encode", str(source)])
    assert photophone.main(["decode", str(tmp_path / "photo.flac")]) == 0
    assert (tmp_path / "photo.decoded.jpg").read_bytes() == source.read_bytes()


def test_jpeg_cannot_be_saved_under_the_wrong_suffix(tmp_path, capsys):
    source, audio = tmp_path / "in.jpg", tmp_path / "a.flac"
    Image.fromarray(smooth_photo(64, 64)).save(source, quality=90)
    photophone.encode(source, audio)
    assert photophone.main(["decode", str(audio), "-o", str(tmp_path / "b.png")]) == 1
    assert ".jpg" in capsys.readouterr().err


@pytest.mark.parametrize("mode,channels", [("L", 1), ("LA", 2), ("RGB", 3), ("RGBA", 4)])
def test_png_is_smaller_and_exact_through_jxl(tmp_path, mode, channels):
    array = gradient(128, 128, channels)
    source, audio = tmp_path / "in.png", tmp_path / "a.flac"
    Image.fromarray(array.squeeze(-1) if channels == 1 else array).save(source, compress_level=0)
    result = photophone.encode(source, audio)
    assert result["after"] < result["before"]
    photophone.decode(audio, tmp_path / "b.png")
    assert np.array_equal(np.asarray(Image.open(tmp_path / "b.png")).reshape(array.shape), array)


def test_jxl_pixels_keep_the_colour_profile(tmp_path):
    audio = tmp_path / "a.flac"
    array = gradient(64, 64, 3)
    photophone.write_jxl_pixels(audio, array, "RGB", profile_bytes())
    restored = photophone.restore(audio)
    assert restored.icc_profile == profile_bytes()
    assert np.array_equal(restored.pixels, array)


def test_incompressible_image_barely_grows(tmp_path):
    source, audio = tmp_path / "in.png", tmp_path / "a.flac"
    Image.fromarray(noise(256, 256, 3)).save(source)
    result = photophone.encode(source, audio)
    assert result["method"] == "stored"
    assert result["after"] < result["before"] * 1.01
    photophone.decode(audio, tmp_path / "b.png")
    assert (tmp_path / "b.png").read_bytes() == source.read_bytes()


def test_payload_with_wrong_kind_is_refused(tmp_path):
    audio = tmp_path / "a.flac"
    photophone.write_payload(audio, 9, b"data", zlib.crc32(b"data"))
    with pytest.raises(photophone.PhotophoneError, match="not made by this tool"):
        photophone.restore(audio)


@functools.cache  # The profile holds its creation time, so build it only once.
def profile_bytes():
    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


@pytest.mark.parametrize("channels", [1, 2, 3, 4])
def test_colour_profile_is_restored_exactly(tmp_path, channels):
    array = gradient(24, 31, channels)
    source = tmp_path / "in.png"
    Image.fromarray(array.squeeze(-1) if channels == 1 else array).save(source, icc_profile=profile_bytes())
    restored = round_trip(tmp_path, source)
    assert restored.info["icc_profile"] == profile_bytes()
    assert np.array_equal(np.asarray(restored).reshape(array.shape), array)


def test_colour_profile_survives_wav_conversion(tmp_path):
    source, audio, wav = tmp_path / "in.png", tmp_path / "a.flac", tmp_path / "a.wav"
    Image.fromarray(gradient(20, 30, 3)).save(source, icc_profile=profile_bytes())
    photophone.main(["encode", str(source), "-o", str(audio)])
    samples, rate = sf.read(audio, dtype="int16", always_2d=True)
    sf.write(wav, samples, rate, subtype="PCM_16")
    assert photophone.main(["decode", str(wav), "-o", str(tmp_path / "b.png")]) == 0
    assert Image.open(tmp_path / "b.png").info["icc_profile"] == profile_bytes()


def test_corrupted_colour_profile_is_detected(tmp_path, capsys):
    source, audio = tmp_path / "in.png", tmp_path / "a.flac"
    photophone.write_audio(audio, gradient(32, 32, 3), "RGB", profile_bytes())
    samples, rate = sf.read(audio, dtype="int16", always_2d=True)
    samples[12, 0] ^= 1
    sf.write(audio, samples, rate, format="FLAC", subtype="PCM_16")
    assert photophone.main(["decode", str(audio), "-o", str(tmp_path / "b.png")]) == 1
    assert "corrupted" in capsys.readouterr().err


def test_files_from_before_colour_profiles_still_decode(tmp_path):
    array = gradient(16, 16, 3)
    audio = tmp_path / "old.flac"
    header = photophone.HEADER.pack(photophone.MAGIC, photophone.MODES.index("RGB"), 16, 16, zlib.crc32(array.tobytes()))
    prefix = np.zeros(18, dtype=np.int16)
    prefix[: len(header)] = np.frombuffer(header, dtype=np.uint8)
    residuals = photophone.pixels_to_residuals(array, "RGB").reshape(-1, 3).astype(np.int16)
    sf.write(audio, np.concatenate([prefix.reshape(-1, 3), residuals]), 44100, format="FLAC", subtype="PCM_16")
    assert np.array_equal(photophone.read_audio(audio)[0], array)


def test_pixel_limit_sends_large_images_down_the_file_path(tmp_path):
    source, audio = tmp_path / "in.png", tmp_path / "a.flac"
    Image.fromarray(gradient(32, 32, 3)).save(source)
    assert photophone.encode(source, audio, max_pixels=100)["method"] == "data"
    audio.unlink()
    photophone.write_audio(audio, gradient(32, 32, 3), "RGB")
    with pytest.raises(photophone.PhotophoneError, match="too large"):
        photophone.read_audio(audio, max_pixels=100)


def test_zero_sized_header_is_refused(tmp_path):
    audio = tmp_path / "a.flac"
    header = photophone.HEADER.pack(photophone.MAGIC, photophone.MODES.index("L"), 0, 0, 0)
    sf.write(audio, np.frombuffer(header, dtype=np.uint8).astype(np.int16), 44100, format="FLAC", subtype="PCM_16")
    with pytest.raises(photophone.PhotophoneError, match="truncated"):
        photophone.read_audio(audio)


def jpeg_with_profile(path):
    Image.fromarray(smooth_photo(96, 96)).save(path, quality=90, icc_profile=b"\x5a" * 200_000)
    return path.read_bytes()


def test_jpeg_segments_split_and_join_exactly(tmp_path):
    data = jpeg_with_profile(tmp_path / "in.jpg")
    stripped, packed = photophone.split_jpeg_segments(data)
    assert b"ICC_PROFILE" not in stripped and packed
    assert photophone.join_jpeg_segments(stripped, packed) == data


def test_jpeg_without_profile_has_nothing_to_split(tmp_path):
    path = tmp_path / "in.jpg"
    Image.fromarray(smooth_photo(32, 32)).save(path)
    data = path.read_bytes()
    assert photophone.split_jpeg_segments(data) == (data, b"")


def test_refused_colour_profile_still_gives_an_identical_jpeg(tmp_path, monkeypatch):
    source, audio = tmp_path / "in.jpg", tmp_path / "a.flac"
    Image.fromarray(smooth_photo()).save(source, quality=90, icc_profile=profile_bytes())
    real_encoder = photophone.pillow_jxl.Encoder

    class PickyEncoder:
        def __init__(self, **options):
            self.encoder = real_encoder(**options)

        def __call__(self, data, *args, **options):
            if b"ICC_PROFILE" in data:
                raise RuntimeError("Input is invalid")
            return self.encoder(data, *args, **options)

    monkeypatch.setattr(photophone.pillow_jxl, "Encoder", PickyEncoder)
    result = photophone.encode(source, audio)
    assert result["method"] == "jpeg" and result["after"] < result["before"]
    photophone.decode(audio, tmp_path / "b.jpg")
    assert (tmp_path / "b.jpg").read_bytes() == source.read_bytes()


def test_max_flag_restores_exactly(tmp_path):
    array = gradient(40, 40, 3)
    source, audio = tmp_path / "in.png", tmp_path / "a.flac"
    Image.fromarray(array).save(source, compress_level=0)
    assert photophone.main(["encode", str(source), "-o", str(audio), "--max"]) == 0
    assert photophone.main(["decode", str(audio), "-o", str(tmp_path / "b.png")]) == 0
    assert np.array_equal(np.asarray(Image.open(tmp_path / "b.png")), array)


def photo(height, width, channels):
    y, x = np.mgrid[:height, :width]
    planes = [128 + 60 * np.sin((x + i * 9) / 11) + 50 * np.cos(y / (7 + i)) for i in range(channels)]
    shaped = np.stack(planes, axis=-1) + RNG.normal(0, 3, (height, width, channels))
    return np.clip(shaped, 0, 255).astype(np.uint8)


def truepix_file(path, array, icc_profile=None):
    photophone.write_truepix(path, array, icc_profile)
    return path


@pytest.mark.parametrize("channels, mode", [(1, "L"), (2, "LA"), (3, "RGB"), (4, "RGBA")])
def test_truepix_method_restores_every_mode(tmp_path, channels, mode):
    array = photo(48, 64, channels)
    restored = photophone.restore(truepix_file(tmp_path / "a.flac", array))
    assert restored.mode == mode
    assert np.array_equal(restored.pixels, array)


def test_truepix_keeps_the_colour_profile(tmp_path):
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    restored = photophone.restore(truepix_file(tmp_path / "a.flac", photo(32, 32, 3), profile))
    assert restored.icc_profile == profile


def test_max_picks_truepix_when_it_is_smallest(tmp_path):
    array = photo(160, 200, 3)
    source, audio = tmp_path / "in.png", tmp_path / "a.flac"
    Image.fromarray(array).save(source)
    result = photophone.encode(source, audio, effort=photophone.JXL_MAX_EFFORT)
    assert result["method"] == "truepix"
    photophone.decode(audio, tmp_path / "b.png")
    assert np.array_equal(np.asarray(Image.open(tmp_path / "b.png")), array)


def test_default_encode_does_not_run_truepix(tmp_path, monkeypatch):
    monkeypatch.setattr(photophone, "run_truepix", lambda *_: pytest.fail("Truepix ran at default effort"))
    source = tmp_path / "in.png"
    Image.fromarray(photo(64, 64, 3)).save(source)
    assert photophone.encode(source, tmp_path / "a.flac")["method"] != "truepix"


def test_max_still_works_without_the_codec(tmp_path, monkeypatch):
    monkeypatch.setenv("TRUEPIX_CORE", str(tmp_path / "missing.exe"))
    source = tmp_path / "in.png"
    Image.fromarray(photo(64, 64, 3)).save(source)
    assert photophone.encode(source, tmp_path / "a.flac", effort=photophone.JXL_MAX_EFFORT)["method"] != "truepix"


def test_decoding_without_the_codec_explains_itself(tmp_path, monkeypatch):
    audio = truepix_file(tmp_path / "a.flac", photo(32, 32, 3))
    monkeypatch.setenv("TRUEPIX_CORE", str(tmp_path / "missing.exe"))
    with pytest.raises(photophone.PhotophoneError, match="Truepix codec was not found"):
        photophone.restore(audio)


def rewrite_truepix(path, array, change):
    """Write a Photophone file whose Truepix payload has been altered, keeping its outer checksum valid."""
    payload = bytearray(photophone.truepix_compress(array))
    change(payload)
    photophone.write_payload(path, photophone.KIND_TRUEPIX, bytes(payload), photophone.content_crc(array, None))
    return path


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p.__setitem__(13, 16),  # 16 tiles per side would need gigabytes
        lambda p: p.__setitem__(12, 9),  # no such channel count
        lambda p: p.__setitem__(slice(0, 4), b"NOPE"),
        lambda p: p.__setitem__(4, p[4] + 1),  # width disagrees with the tile inside
        lambda p: p.__setitem__(22, p[22] ^ 1),  # tile header says it is a different size
        lambda p: p.__setitem__(30, 1),  # tile header disagrees about channels
        lambda p: p.__setitem__(31, 7),  # no such coding mode
        lambda p: p.__setitem__(len(p) // 2, p[len(p) // 2] ^ 255),  # damaged coded data
    ],
)
def test_damaged_truepix_is_refused(tmp_path, change):
    audio = rewrite_truepix(tmp_path / "a.flac", photo(32, 32, 3), change)
    with pytest.raises(photophone.PhotophoneError):
        photophone.restore(audio)


def test_tile_header_errors_are_reported_as_damage(tmp_path):
    array = photo(32, 32, 3)
    for offset, value, in ((22, 33), (31, 7)):
        audio = rewrite_truepix(tmp_path / "a.flac", array, lambda p: p.__setitem__(offset, value))
        with pytest.raises(photophone.PhotophoneError, match="damaged Truepix file"):
            photophone.restore(audio)


def test_grid_mode_needs_three_channels(tmp_path):
    audio = rewrite_truepix(tmp_path / "a.flac", photo(32, 32, 1), lambda p: p.__setitem__(31, 1))
    with pytest.raises(photophone.PhotophoneError, match="damaged Truepix file"):
        photophone.restore(audio)


def test_truepix_dimensions_are_checked_before_decoding(tmp_path, monkeypatch):
    audio = rewrite_truepix(tmp_path / "a.flac", photo(32, 32, 3), lambda p: p.__setitem__(slice(4, 8), (1 << 20).to_bytes(4, "little")))
    monkeypatch.setattr(photophone, "run_truepix", lambda *_: pytest.fail("decoded a file over the limit"))
    with pytest.raises(photophone.PhotophoneError, match="too large"):
        photophone.restore(audio, max_pixels=1000)


def carries_exactly(tmp_path, source):
    audio, back = tmp_path / "a.flac", tmp_path / "back.bin"
    assert photophone.main(["encode", str(source), "-o", str(audio)]) == 0
    assert photophone.main(["decode", str(audio), "-o", str(back)]) == 0
    assert back.read_bytes() == source.read_bytes()


@pytest.mark.parametrize("name, data", [
    ("notes.txt", b"plain text " * 1000),
    ("already.zip", bytes(np.random.default_rng(3).integers(0, 256, 5000, dtype=np.uint8))),
    ("empty.bin", b""),
    ("odd", b"\x00\xff" * 7 + b"\x01"),
    ("many.dots.tar.gz", b"x" * 301),
])
def test_any_file_round_trip(tmp_path, name, data):
    source = tmp_path / name
    source.write_bytes(data)
    carries_exactly(tmp_path, source)


def test_decode_names_the_file_after_the_original(tmp_path):
    source = tmp_path / "report.final.pdf"
    source.write_bytes(b"pdf" * 200)
    audio = tmp_path / "a.flac"
    photophone.encode(source, audio)
    assert photophone.decode(audio)["path"].name == "report.final.decoded.pdf"


def test_stored_name_cannot_leave_the_folder(tmp_path):
    source, audio = tmp_path / "in.bin", tmp_path / "a.flac"
    source.write_bytes(b"data" * 100)
    photophone.encode(source, audio, name="../../evil.txt")
    result = photophone.decode(audio)
    assert result["path"].parent == tmp_path
    assert result["name"] == "evil.txt"


def test_expansion_bomb_is_refused_before_expanding(tmp_path):
    audio = tmp_path / "a.flac"
    payload = photophone.pack_data(b"\0" * 10_000_000, "bomb.bin")
    photophone.write_payload(audio, photophone.KIND_DATA, payload, zlib.crc32(b"\0" * 10_000_000))
    assert len(payload) < 5000
    with pytest.raises(photophone.PhotophoneError, match="limit"):
        photophone.restore(audio, max_pixels=1000)


def test_lying_size_is_refused(tmp_path):
    audio = tmp_path / "a.flac"
    payload = bytearray(photophone.pack_data(b"hello world " * 1000, "a.txt"))
    payload[1] = (payload[1] + 1) & 255
    photophone.write_payload(audio, photophone.KIND_DATA, bytes(payload), zlib.crc32(b"hello world " * 1000))
    with pytest.raises(photophone.PhotophoneError, match="corrupted"):
        photophone.restore(audio)


def test_damaged_data_is_refused(tmp_path):
    audio = tmp_path / "a.flac"
    original = bytes(range(256)) * 40
    payload = bytearray(photophone.pack_data(original, "a.bin"))
    payload[-1] ^= 1
    photophone.write_payload(audio, photophone.KIND_DATA, bytes(payload), zlib.crc32(original))
    with pytest.raises(photophone.PhotophoneError, match="corrupted"):
        photophone.restore(audio)
