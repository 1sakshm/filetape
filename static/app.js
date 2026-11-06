"use strict";

const MAX_UPLOAD_BYTES = 40 * 1024 * 1024;
const MAX_BATCH_BYTES = 500 * 1024 * 1024;
const TARGET_RMS = 0.1; // Playback level, as a fraction of full scale.
const METHOD_NOTES = {
  jpeg: "Decoding rebuilds the original JPEG byte for byte, including its metadata.",
  jxl: "Every pixel was checked against the original.",
  residual: "Every pixel was checked against the original.",
  stored: "Decoding returns the original file unchanged.",
};

const $ = (id) => document.getElementById(id);
const objectUrls = new Set();
let audioBytes = null;
let audioBlob = null;
let baseName = "image";
let requestNumber = 0;
let player = null;

function objectUrl(blob) {
  const url = URL.createObjectURL(blob);
  objectUrls.add(url);
  return url;
}

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
}

function formatSeconds(seconds) {
  if (seconds < 60) return `${seconds.toFixed(1)} s`;
  return `${Math.floor(seconds / 60)} min ${Math.round(seconds % 60)} s`;
}

function showStatus(message) {
  $("status").textContent = message;
  $("status").hidden = !message;
}

function showError(message) {
  $("error").textContent = message;
  $("error").hidden = !message;
}

function stopPlayback() {
  if (!player) return;
  player.audio.pause();
  player.context.close();
  player = null;
}

function resetResult() {
  stopPlayback();
  objectUrls.forEach((url) => URL.revokeObjectURL(url));
  objectUrls.clear();
  audioBytes = audioBlob = null;
  $("result").hidden = true;
  $("restored-panel").hidden = true;
  $("download-image").hidden = true;
  $("download-zip").hidden = true;
  $("rebuild").hidden = false;
  $("no-preview").hidden = true;
  $("original").hidden = false;
  $("play").textContent = "Play";
  $("stats").replaceChildren();
  $("verdict").textContent = "";
}

async function post(path, file) {
  const form = new FormData();
  form.append("file", file);
  const response = await fetch(path, { method: "POST", body: form });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new Error(body?.error ?? "The server couldn't convert this file.");
  }
  return {
    blob: await response.blob(),
    result: JSON.parse(response.headers.get("X-Photophone-Result")),
  };
}

function addStat(label, value, className) {
  const row = document.createElement("div");
  const term = document.createElement("dt");
  const detail = document.createElement("dd");
  term.textContent = label;
  detail.textContent = value;
  if (className) detail.className = className;
  row.append(term, detail);
  $("stats").append(row);
}

function showRestored(blob, result) {
  const url = objectUrl(blob);
  $("restored").src = url;
  $("restored-panel").hidden = false;
  const link = $("download-image");
  link.href = url;
  link.download = `${baseName}.decoded${result.suffix}`;
  link.hidden = false;
  $("rebuild").hidden = true;
  const profile = result.icc_profile ? " Colour profile restored." : "";
  $("verdict").textContent =
    `Rebuilt ${result.width} × ${result.height} image. Checksum matches the original.${profile}`;
}

function drawWave(buffer) {
  const canvas = $("wave");
  const scale = window.devicePixelRatio || 1;
  const width = Math.max(1, Math.round(canvas.clientWidth * scale));
  const height = Math.max(1, Math.round(canvas.clientHeight * scale));
  canvas.width = width;
  canvas.height = height;

  const channel = buffer.getChannelData(0);
  const step = Math.max(1, Math.floor(channel.length / width));
  const peaks = new Float32Array(width);
  for (let x = 0; x < width; x++) {
    const start = x * step;
    let peak = 0;
    for (let i = start; i < Math.min(start + step, channel.length); i++) {
      peak = Math.max(peak, Math.abs(channel[i]));
    }
    peaks[x] = peak;
  }
  // The stored header is louder than the audio, so scale to a high percentile.
  const sorted = Array.from(peaks).sort((a, b) => a - b);
  const top = sorted[Math.floor(width * 0.98)] || 1;

  const context = canvas.getContext("2d");
  context.fillStyle = getComputedStyle(document.documentElement).getPropertyValue("--accent");
  for (let x = 0; x < width; x++) {
    const bar = Math.max(1, Math.min(1, peaks[x] / top) * height * 0.9);
    context.fillRect(x, (height - bar) / 2, 1, bar);
  }
}

function rootMeanSquare(buffer) {
  const channel = buffer.getChannelData(0);
  const step = Math.max(1, Math.floor(channel.length / 100000));
  let sum = 0;
  let count = 0;
  for (let i = 0; i < channel.length; i += step) {
    sum += channel[i] * channel[i];
    count++;
  }
  return Math.sqrt(sum / count) || 1;
}

async function loadAudio(blob) {
  audioBlob = blob;
  audioBytes = await blob.arrayBuffer();
  const context = new OfflineAudioContext(1, 1, 44100);
  try {
    const buffer = await context.decodeAudioData(audioBytes.slice(0));
    drawWave(buffer);
    return rootMeanSquare(buffer);
  } catch {
    return null; // Downloads still work if this browser can't decode the audio.
  }
}

async function togglePlayback(rms) {
  if (player) {
    if (player.audio.paused) {
      await player.context.resume();
      await player.audio.play();
    } else {
      player.audio.pause();
    }
    return;
  }
  const audio = new Audio(objectUrl(audioBlob));
  const context = new AudioContext();
  const gain = context.createGain();
  gain.gain.value = rms ? TARGET_RMS / rms : 1;
  context.createMediaElementSource(audio).connect(gain).connect(context.destination);
  audio.addEventListener("play", () => { $("play").textContent = "Pause"; });
  audio.addEventListener("pause", () => { $("play").textContent = "Play"; });
  player = { audio, context };
  await audio.play();
}

async function handleEncode(file, run) {
  showStatus("Encoding and checking every pixel…");
  const { blob, result } = await post("/api/encode", file);
  if (run !== requestNumber) return;
  $("original-label").textContent = "Original image";
  $("original").src = objectUrl(file);
  $("original-panel").hidden = false;
  $("audio-panel").hidden = false;
  $("download-audio").hidden = false;
  $("download-audio").href = objectUrl(blob);
  $("download-audio").download = `${baseName}.flac`;
  $("result").hidden = false;

  const change = (result.after - result.before) / result.before;
  const percent = `${change > 0 ? "+" : ""}${(change * 100).toFixed(1)}%`;
  addStat("Original", formatBytes(result.before));
  addStat("Audio", formatBytes(result.after));
  addStat("Change", percent, change < 0 ? "smaller" : "larger");
  addStat("Length", formatSeconds(result.seconds));

  const profile = result.icc_profile ? " The colour profile is stored too." : "";
  $("verdict").textContent = change < 0
    ? `${METHOD_NOTES[result.method]}${profile}`
    : `This image couldn't be compressed further, so it was stored unchanged. The audio is slightly larger than the original.`;

  const rms = await loadAudio(blob);
  if (run !== requestNumber) return;
  $("play").onclick = () => {
    showError("");
    togglePlayback(rms).catch(() =>
      showError("This browser couldn't play the audio. You can still download it."));
  };
}

async function handleDecode(file, run) {
  showStatus("Rebuilding the image…");
  const { blob, result } = await post("/api/decode", file);
  if (run !== requestNumber) return;
  $("original-panel").hidden = true;
  $("audio-panel").hidden = true;
  $("download-audio").hidden = true;
  $("result").hidden = false;
  addStat("Audio", formatBytes(file.size));
  addStat("Image", formatBytes(blob.size));
  addStat("Dimensions", `${result.width} × ${result.height}`);
  showRestored(blob, result);
}

function batchSummary(result) {
  const parts = [];
  if (result.compressed) parts.push(`Compressed ${result.compressed} images into audio.`);
  if (result.restored) parts.push(`Rebuilt ${result.restored} images from audio.`);
  if (!result.compressed && !result.restored) parts.push("Nothing in this zip could be made smaller.");
  if (result.copied) parts.push(`${result.copied} files were copied unchanged.`);
  if (result.skipped) parts.push(`${result.skipped} files could not be read and are missing from the new zip.`);
  if (result.problems.length) parts.push(`Check: ${result.problems.join(", ")}.`);
  return parts.join(" ");
}

async function handleBatch(file, run) {
  showStatus("Converting every image in the zip. Large folders can take several minutes…");
  const { blob, result } = await post("/api/batch", file);
  if (run !== requestNumber) return;
  $("original-panel").hidden = true;
  $("audio-panel").hidden = true;
  $("download-audio").hidden = true;
  $("rebuild").hidden = true;
  $("download-zip").hidden = false;
  $("download-zip").href = objectUrl(blob);
  $("download-zip").download = `${baseName}.photophone.zip`;
  $("result").hidden = false;

  const change = (result.after - result.before) / result.before;
  addStat("Original zip", formatBytes(result.before));
  addStat("New zip", formatBytes(result.after));
  addStat("Change", `${change > 0 ? "+" : ""}${(change * 100).toFixed(1)}%`, change < 0 ? "smaller" : "larger");
  addStat("Files", String(result.compressed + result.restored + result.copied));
  $("verdict").textContent = batchSummary(result);
}

async function handleFile(file) {
  const run = ++requestNumber;
  resetResult();
  showError("");
  const isZip = /\.zip$/i.test(file.name);
  if (file.size > (isZip ? MAX_BATCH_BYTES : MAX_UPLOAD_BYTES)) {
    showError(`Files can be up to ${isZip ? 500 : 40} MB.`);
    return;
  }
  baseName = file.name.replace(/\.[^.]+$/, "") || "image";
  const isAudio = /\.(flac|wav)$/i.test(file.name) || file.type.startsWith("audio/");
  try {
    if (isZip) await handleBatch(file, run);
    else await (isAudio ? handleDecode(file, run) : handleEncode(file, run));
  } catch (error) {
    if (run === requestNumber) showError(error.message);
  } finally {
    if (run === requestNumber) showStatus("");
  }
}

async function rebuild() {
  const run = requestNumber;
  const button = $("rebuild");
  button.disabled = true;
  showError("");
  showStatus("Rebuilding the image from the audio…");
  try {
    const file = new File([audioBlob], `${baseName}.flac`, { type: "audio/flac" });
    const { blob, result } = await post("/api/decode", file);
    if (run === requestNumber) showRestored(blob, result);
  } catch (error) {
    if (run === requestNumber) showError(error.message);
  } finally {
    button.disabled = false;
    if (run === requestNumber) showStatus("");
  }
}

// A row of coloured pixels that turns into a waveform, as a picture of the idea.
function drawStrip() {
  const canvas = $("strip");
  const scale = window.devicePixelRatio || 1;
  const width = Math.round(canvas.clientWidth * scale);
  const height = Math.round(canvas.clientHeight * scale);
  canvas.width = width;
  canvas.height = height;
  const context = canvas.getContext("2d");
  const cell = 8 * scale;
  const columns = Math.ceil(width / cell);
  for (let column = 0; column < columns; column++) {
    const progress = column / columns;
    const wave = Math.abs(Math.sin(column * 0.41) * 0.6 + Math.sin(column * 0.13) * 0.4);
    const size = height * (1 - progress) + Math.max(cell, height * wave) * progress;
    const hue = (column * 2.6 + Math.sin(column * 0.2) * 40) % 360;
    context.fillStyle = `hsl(${hue} 62% 55%)`;
    const rows = Math.ceil(size / cell);
    for (let row = 0; row < rows; row++) {
      const y = (height - rows * cell) / 2 + row * cell;
      context.globalAlpha = 0.55 + 0.45 * Math.abs(Math.sin(column * 7.3 + row * 3.1));
      context.fillRect(column * cell, y, cell - scale, cell - scale);
    }
  }
}

const drop = $("drop");
["dragenter", "dragover"].forEach((name) => drop.addEventListener(name, (event) => {
  event.preventDefault();
  drop.classList.add("over");
}));
["dragleave", "drop"].forEach((name) => drop.addEventListener(name, (event) => {
  event.preventDefault();
  drop.classList.remove("over");
}));
drop.addEventListener("drop", (event) => {
  const file = event.dataTransfer.files[0];
  if (file) handleFile(file);
});

$("choose").addEventListener("click", () => $("file").click());
$("file").addEventListener("change", (event) => {
  const file = event.target.files[0];
  event.target.value = "";
  if (file) handleFile(file);
});
$("rebuild").addEventListener("click", rebuild);
$("original").addEventListener("error", () => {
  $("original").hidden = true;
  $("no-preview").hidden = false;
});
window.addEventListener("dragover", (event) => event.preventDefault());
window.addEventListener("drop", (event) => event.preventDefault());
window.addEventListener("resize", drawStrip);
drawStrip();
