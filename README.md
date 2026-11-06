# Photophone

Stores an image losslessly as a playable FLAC audio file, then restores it pixel for pixel.

## Usage

```
uv run photophone encode photo.png             # writes photo.flac
uv run photophone decode photo.flac            # writes photo.decoded.png (.jpg for a JPEG)
uv run photophone encode photo.png -o a.flac   # choose the output path
uv run photophone encode photo.png --max       # smallest output, also tries the Truepix codec
```

To use the website, run `uv run photophone-web` and open http://127.0.0.1:8000. It accepts files up to
40 MB and 50 megapixels. Set `HOST` and `PORT` to change where it listens.

Existing files are only overwritten with `--force`. Encoding decodes the result and compares it
with the source before saving, and decoding checks a CRC-32 of the pixels.

On the website, upload a zip of a folder and download a new zip with the same folders. Images become `name.png.flac`, and uploading that zip turns them back. Images that would not get smaller, and other files, are copied unchanged. Zips can be up to 500 MB and 1,000 files.

## How it works

`encode` tries each method that applies, verifies the result and keeps the smallest:

| Method   | Used for   | What is stored                                                     | Restores                 |
|----------|------------|--------------------------------------------------------------------|--------------------------|
| jpeg     | JPEG       | The JPEG's own data, recompressed as JPEG XL                       | The exact original file  |
| jxl      | Other      | Lossless JPEG XL of the pixels                                     | Pixels and colour profile      |
| residual | Other      | Prediction errors as audio samples, compressed by FLAC             | Pixels and colour profile      |
| stored   | Everything | The original file, unchanged                                       | The exact original file  |

JPEG XL does the compressing in the jpeg and jxl methods, and FLAC is the carrier: their bytes
become 16-bit samples, which adds about 0.2%. This audio is loud white noise. The residual method
produces a very quiet hiss, around -68 dBFS, but it was never the smallest on the sample images.

The audio can be converted between FLAC and WAV, but lossy formats such as MP3 or AAC, or playing
and re-recording it, destroy the image.

## Expected size

Measured on 23 library sample images, against the size of the original file. All 23 came out
smaller and were restored exactly, and the total dropped by 30.8%:

| Image type             | Smaller than the original | Result         |
|------------------------|---------------------------|----------------|
| Colour PNG, with logos | 8 of 8                    | 27-59% smaller |
| Grayscale PNG          | 9 of 9                    | 6-45% smaller  |
| JPEG                   | 6 of 6                    | 13-22% smaller |

Results depend on the image, and a file that can't be compressed further grows by about 0.2%.

The tool warns when the output is not smaller than the input.

## Truepix codec

`--max` also tries [Truepix](../Truepix), a separate lossless image codec, and keeps its result
when it wins. It takes about 15 to 35 seconds each way for a 3-megapixel image, on top of the
other methods. Truepix is optional: without it, `--max` uses the other methods. The website can
decode these files, but encodes at default effort, so it does not create them.

Files made with Truepix need it to decode. Photophone looks for `truepix-core` in `TRUEPIX_CORE`,
then on `PATH`, then in a `Truepix` folder beside this one. See the Truepix README to build it.

## Limits

- Only 8-bit images are supported. 16-bit, CMYK and animated images are refused.
- Palette and 1-bit images are restored as RGB(A) or grayscale with the same visible pixels.
- JPEGs and stored files return byte for byte. Other images keep their pixels and ICC colour
  profile, but lose EXIF and other metadata, and the decoded PNG is not byte-identical.

## Tests

```
uv run pytest
```
