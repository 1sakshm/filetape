"use strict";

// The whole conversion, running on the visitor's device. It writes the same format as filetape.py,
// so audio from this page opens in the desktop tool, and the page opens the desktop tool's files
// except those that need JPEG XL or LZMA, which browsers don't have.

const PAYLOAD_MAGIC = "PXF1", HEADER = 17;  // magic, kind, size, profile size, CRC-32
const KIND_FILE = 3, KIND_DATA = 6;
const DATA_RAW = 0, DATA_LZMA = 1, DATA_DEFLATE = 2, DATA_JPEG = 3;
const BLOCK = 4096, RATE = 44100;
const encoder = new TextEncoder(), decoder = new TextDecoder();

const CRC_TABLE = Array.from({ length: 256 }, (_, n) => {
  for (let k = 0; k < 8; k++) n = n & 1 ? 0xedb88320 ^ (n >>> 1) : n >>> 1;
  return n >>> 0;
});
function crc32(bytes) {
  let crc = 0xffffffff;
  for (let i = 0; i < bytes.length; i++) crc = CRC_TABLE[(crc ^ bytes[i]) & 255] ^ (crc >>> 8);
  return (crc ^ 0xffffffff) >>> 0;
}
const CRC8 = Array.from({ length: 256 }, (_, n) => {
  for (let k = 0; k < 8; k++) n = n & 0x80 ? ((n << 1) ^ 7) & 255 : (n << 1) & 255;
  return n;
});
const CRC16 = Array.from({ length: 256 }, (_, n) => {
  let c = n << 8;
  for (let k = 0; k < 8; k++) c = c & 0x8000 ? ((c << 1) ^ 0x8005) & 0xffff : (c << 1) & 0xffff;
  return c;
});

// FLAC. Good compression looks random, so every block is stored verbatim.
function flacEncode(samples) {
  const blocks = Math.ceil(samples.length / BLOCK);
  const out = new Uint8Array(42 + samples.length * 2 + blocks * 32);
  let at = 0;
  out.set([0x66, 0x4c, 0x61, 0x43, 0x80, 0, 0, 34], 0);
  at = 8;
  const info = new DataView(out.buffer, at, 34);
  info.setUint16(0, BLOCK); info.setUint16(2, BLOCK);
  // Sample rate (20 bits), channels - 1 (3), bits - 1 (5), total samples (36).
  info.setUint32(10, (RATE << 12 | (15 << 4) | Math.floor(samples.length / 2 ** 32)) >>> 0);
  info.setUint32(14, samples.length >>> 0);
  at += 34;
  for (let index = 0; index < blocks; index++) {
    const start = at, length = Math.min(BLOCK, samples.length - index * BLOCK);
    out[at++] = 0xff; out[at++] = 0xf8;
    out[at++] = (length === BLOCK ? 0xc0 : 0x70) | 9;
    out[at++] = 0x08;
    if (index < 0x80) out[at++] = index;
    else {
      let bytes = 2, limit = 0x800;
      while (index >= limit) { bytes++; limit *= 32; }
      out[at++] = ((0xff00 >> bytes) & 255) | Math.floor(index / 2 ** ((bytes - 1) * 6));
      for (let k = bytes - 2; k >= 0; k--) out[at++] = 0x80 | (Math.floor(index / 2 ** (k * 6)) & 63);
    }
    if (length !== BLOCK) { out[at++] = (length - 1) >> 8; out[at++] = (length - 1) & 255; }
    let crc8 = 0;
    for (let i = start; i < at; i++) crc8 = CRC8[crc8 ^ out[i]];
    out[at++] = crc8;
    out[at++] = 0x02;  // a verbatim subframe
    const first = index * BLOCK;
    for (let i = 0; i < length; i++) {
      const v = samples[first + i];
      out[at++] = (v >> 8) & 255;
      out[at++] = v & 255;
    }
    let crc16 = 0;
    for (let i = start; i < at; i++) crc16 = ((crc16 << 8) & 0xffff) ^ CRC16[(crc16 >> 8) ^ out[i]];
    out[at++] = crc16 >> 8;
    out[at++] = crc16 & 255;
  }
  return out.subarray(0, at);
}

class Bits {
  constructor(bytes, at) { this.b = bytes; this.at = at; this.bit = 0; }
  read(count) {
    let v = 0;
    while (count > 0) {
      if (this.at >= this.b.length) throw new Error("bad");
      const room = 8 - this.bit, take = Math.min(room, count);
      v = v * (1 << take) + ((this.b[this.at] >> (room - take)) & ((1 << take) - 1));
      this.bit += take; count -= take;
      if (this.bit === 8) { this.bit = 0; this.at++; }
    }
    return v;
  }
  signed(count) { const v = this.read(count); return v >= 2 ** (count - 1) ? v - 2 ** count : v; }
  unary() { let n = 0; while (!this.read(1)) n++; return n; }
  align() { if (this.bit) { this.bit = 0; this.at++; } }
}

function residual(bits, out, offset, order, size) {
  const method = bits.read(2);
  if (method > 1) throw new Error("bad");
  const partitions = 1 << bits.read(4);
  let at = offset;
  for (let p = 0; p < partitions; p++) {
    const count = size / partitions - (p === 0 ? order : 0);
    let parameter = bits.read(method ? 5 : 4);
    if (parameter === (method ? 31 : 15)) {
      const raw = bits.read(5);
      for (let i = 0; i < count; i++) out[at++] = raw ? bits.signed(raw) : 0;
    } else {
      for (let i = 0; i < count; i++) {
        const u = bits.unary() * 2 ** parameter + (parameter ? bits.read(parameter) : 0);
        out[at++] = u % 2 ? -(u + 1) / 2 : u / 2;
      }
    }
  }
}

const FIXED = [[], [1], [2, -1], [3, -3, 1], [4, -6, 4, -1]];
function subframe(bits, size, depth) {
  bits.read(1);
  const type = bits.read(6);
  let wasted = 0;
  if (bits.read(1)) { wasted = bits.unary() + 1; depth -= wasted; }
  const out = new Int32Array(size);
  if (type === 0) out.fill(bits.signed(depth));
  else if (type === 1) for (let i = 0; i < size; i++) out[i] = bits.signed(depth);
  else {
    let order, coefs, shift = 0;
    if (type >= 8 && type <= 12) {
      order = type - 8;
      coefs = FIXED[order];
    } else if (type >= 32) {
      order = type - 31;
    } else throw new Error("bad");
    for (let i = 0; i < order; i++) out[i] = bits.signed(depth);
    if (type >= 32) {
      const precision = bits.read(4) + 1;
      shift = bits.signed(5);
      coefs = [];
      for (let i = 0; i < order; i++) coefs.push(bits.signed(precision));
    }
    residual(bits, out, order, order, size);
    const scale = 2 ** shift;
    for (let i = order; i < size; i++) {
      let sum = 0;
      for (let k = 0; k < order; k++) sum += coefs[k] * out[i - k - 1];
      out[i] += Math.floor(sum / scale);
    }
  }
  if (wasted) for (let i = 0; i < size; i++) out[i] *= 2 ** wasted;
  return out;
}

function flacDecode(bytes) {
  if (decoder.decode(bytes.subarray(0, 4)) !== "fLaC") throw new Error("bad");
  let at = 4, last = false, channels = 1, total = 0, fixedDepth = 16;
  while (!last) {
    last = !!(bytes[at] & 0x80);
    const kind = bytes[at] & 0x7f, size = bytes[at + 1] << 16 | bytes[at + 2] << 8 | bytes[at + 3];
    if (kind === 0) {
      channels = ((bytes[at + 16] >> 1) & 7) + 1;
      fixedDepth = (((bytes[at + 16] & 1) << 4) | (bytes[at + 17] >> 4)) + 1;
      total = (bytes[at + 17] & 15) * 2 ** 32 + new DataView(bytes.buffer, bytes.byteOffset + at + 18, 4).getUint32(0);
    }
    at += 4 + size;
  }
  if (fixedDepth !== 16) throw new Error("bad");
  const out = new Int16Array(total * channels);
  let filled = 0;
  const bits = new Bits(bytes, at);
  while (filled < out.length && bits.at + 2 < bytes.length) {
    if (bits.read(14) !== 0x3ffe) throw new Error("bad");
    bits.read(2);
    const sizeCode = bits.read(4), rateCode = bits.read(4), assign = bits.read(4);
    bits.read(4);
    let lead = bits.read(8);
    while (lead >= 0xc0) { bits.read(8); lead = (lead << 1) & 255; }
    let size = sizeCode === 1 ? 192 : sizeCode <= 5 ? 576 << (sizeCode - 2) : sizeCode >= 8 ? 256 << (sizeCode - 8) : 0;
    if (sizeCode === 6) size = bits.read(8) + 1;
    if (sizeCode === 7) size = bits.read(16) + 1;
    if (rateCode === 12) bits.read(8);
    if (rateCode === 13 || rateCode === 14) bits.read(16);
    bits.read(8);
    const count = assign < 8 ? assign + 1 : 2, parts = [];
    for (let c = 0; c < count; c++) parts.push(subframe(bits, size, 16 + (assign === 8 && c === 1 || assign === 9 && c === 0 || assign === 10 && c === 1 ? 1 : 0)));
    if (assign === 8) for (let i = 0; i < size; i++) parts[1][i] = parts[0][i] - parts[1][i];
    if (assign === 9) for (let i = 0; i < size; i++) parts[0][i] += parts[1][i];
    if (assign === 10) for (let i = 0; i < size; i++) {
      const mid = parts[0][i] * 2 + (parts[1][i] & 1), side = parts[1][i];
      parts[0][i] = (mid + side) >> 1;
      parts[1][i] = (mid - side) >> 1;
    }
    bits.align();
    bits.read(16);
    for (let i = 0; i < size; i++) for (let c = 0; c < count; c++) out[filled++] = parts[c][i];
  }
  if (filled !== out.length) throw new Error("bad");
  return out;
}

function wavDecode(bytes) {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.length);
  for (let at = 12; at + 8 <= bytes.length;) {
    const size = view.getUint32(at + 4, true);
    if (decoder.decode(bytes.subarray(at, at + 4)) === "data") {
      const out = new Int16Array(Math.floor(size / 2));
      for (let i = 0; i < out.length; i++) out[i] = view.getInt16(at + 8 + i * 2, true);
      return out;
    }
    at += 8 + size + (size & 1);
  }
  throw new Error("bad");
}

async function squeeze(stream, bytes) {
  const piped = new Blob([bytes]).stream().pipeThrough(stream);
  return new Uint8Array(await new Response(piped).arrayBuffer());
}
const deflate = (bytes) => squeeze(new CompressionStream("deflate-raw"), bytes);
const inflate = (bytes) => squeeze(new DecompressionStream("deflate-raw"), bytes);

// The Truepix codec, for JPEGs.
let wasmModule = null;
async function truepix() {
  wasmModule ??= await WebAssembly.compile(await (await fetch("truepix.wasm")).arrayBuffer());
  const stubs = {};
  for (const item of WebAssembly.Module.imports(wasmModule)) {
    stubs[item.module] ??= {};
    stubs[item.module][item.name] = item.name === "proc_exit" ? (code) => { throw new Error(`exit ${code}`); } : () => 0;
  }
  const instance = await WebAssembly.instantiate(wasmModule, stubs);
  instance.exports._initialize?.();
  return instance.exports;
}
async function truepixCall(name, bytes) {
  const w = await truepix(), at = w.tp_alloc(bytes.length);
  new Uint8Array(w.memory.buffer, at, bytes.length).set(bytes);
  const size = w[name](at, bytes.length);
  return size ? new Uint8Array(w.memory.buffer, w.tp_packed(), size).slice() : null;
}

function same(a, b) {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
  return true;
}

function safeName(name) {
  const plain = name.replaceAll("\\", "/").split("/").pop();
  const kept = [...plain].filter((ch) => ch >= " " && !'<>:"|?*'.includes(ch)).join("").replace(/^[ .]+|[ .]+$/g, "");
  return kept || "file";
}

function packAudio(kind, payload, crc) {
  const head = new Uint8Array(HEADER), view = new DataView(head.buffer);
  head.set(encoder.encode(PAYLOAD_MAGIC));
  head[4] = kind;
  view.setUint32(5, payload.length, true);
  view.setUint32(9, 0, true);
  view.setUint32(13, crc, true);
  const pad = new Uint8Array(payload.length + (payload.length & 1));
  pad.set(payload);
  const samples = new Int16Array(HEADER + pad.length / 2), words = new DataView(pad.buffer);
  for (let i = 0; i < HEADER; i++) samples[i] = head[i];
  for (let i = 0; i < pad.length / 2; i++) samples[HEADER + i] = words.getInt16(i * 2, true);
  return { audio: flacEncode(samples), seconds: samples.length / RATE };
}

function packData(original, name, method, body) {
  const title = encoder.encode(name).subarray(0, 1024), out = new Uint8Array(11 + title.length + body.length);
  const view = new DataView(out.buffer);
  out[0] = method;
  view.setBigUint64(1, BigInt(original.length), true);
  view.setUint16(9, title.length, true);
  out.set(title, 11);
  out.set(body, 11 + title.length);
  return out;
}

function readAudio(bytes) {
  let samples;
  try {
    const tag = decoder.decode(bytes.subarray(0, 4));
    samples = tag === "RIFF" ? wavDecode(bytes) : flacDecode(bytes);
  } catch {
    throw new Error("This file is not a readable audio file.");
  }
  const head = new Uint8Array(HEADER);
  for (let i = 0; i < HEADER; i++) {
    if (samples[i] < 0 || samples[i] > 255) throw new Error("This audio was not made by this tool.");
    head[i] = samples[i];
  }
  const magic = decoder.decode(head.subarray(0, 4));
  if (magic === "PXL1") throw new Error("This audio was made by the desktop Filetape. Open it there.");
  if (magic !== PAYLOAD_MAGIC) throw new Error("This audio was not made by this tool.");
  const view = new DataView(head.buffer), size = view.getUint32(5, true), iccSize = view.getUint32(9, true);
  const start = HEADER + iccSize, body = samples.subarray(start);
  if (body.length !== Math.ceil(size / 2)) throw new Error("This audio is truncated or corrupted.");
  const raw = new Uint8Array(body.length * 2), words = new DataView(raw.buffer);
  for (let i = 0; i < body.length; i++) words.setInt16(i * 2, body[i], true);
  return { kind: head[4], crc: view.getUint32(13, true), payload: raw.subarray(0, size) };
}

async function encodeFile(original, givenName) {
  const name = safeName(givenName);
  let method = "data", payload = null;
  if (original[0] === 0xff && original[1] === 0xd8) {
    const packed = await truepixCall("tp_jpeg_pack", original).catch(() => null);
    if (packed) {
      const back = await truepixCall("tp_jpeg_unpack", packed);
      if (back && same(back, original)) { payload = packData(original, name, DATA_JPEG, packed); method = "jpeg"; }
    }
  }
  if (!payload) {
    const small = await deflate(original);
    payload = small.length < original.length ? packData(original, name, DATA_DEFLATE, small) : packData(original, name, DATA_RAW, original);
  }
  const { audio, seconds } = packAudio(KIND_DATA, payload, crc32(original));
  const check = await decodeFile(audio);
  if (!same(check.bytes, original)) throw new Error("The check failed, so nothing was written.");
  return { audio, result: { before: original.length, after: audio.length, method, seconds, name, icc_profile: false } };
}

function sniff(bytes) {
  if (bytes[0] === 0xff && bytes[1] === 0xd8) return "restored.jpg";
  if (bytes[0] === 0x89 && bytes[1] === 0x50) return "restored.png";
  if (bytes[0] === 0x47 && bytes[1] === 0x49) return "restored.gif";
  return "restored.bin";
}

async function decodeFile(audio) {
  const { kind, crc, payload } = readAudio(audio);
  let name, bytes;
  if (kind === KIND_FILE) {
    bytes = payload;
    name = sniff(bytes);
  } else if (kind === KIND_DATA) {
    const view = new DataView(payload.buffer, payload.byteOffset, payload.length);
    const method = payload[0], size = Number(view.getBigUint64(1, true)), length = view.getUint16(9, true);
    if (size > 2 ** 30) throw new Error("This audio would expand to more than 1 GB.");
    name = safeName(decoder.decode(payload.subarray(11, 11 + length)));
    const body = payload.subarray(11 + length);
    if (method === DATA_RAW) bytes = body;
    else if (method === DATA_DEFLATE) bytes = await inflate(body).catch(() => null);
    else if (method === DATA_JPEG) bytes = await truepixCall("tp_jpeg_unpack", body).catch(() => null);
    else if (method === DATA_LZMA) throw new Error("This audio was made by the desktop Filetape. Open it there.");
    if (!bytes || bytes.length !== size) throw new Error("This audio is corrupted: checksum mismatch.");
  } else {
    throw new Error("This audio was made by the desktop Filetape. Open it there.");
  }
  if (crc32(bytes) !== crc) throw new Error("This audio is corrupted: checksum mismatch.");
  return { bytes, result: { name, size: bytes.length } };
}

// Zip folders.
function zipEntries(bytes) {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.length);
  let end = bytes.length - 22;
  while (end >= 0 && view.getUint32(end, true) !== 0x06054b50) end--;
  if (end < 0) throw new Error("This file is not a readable zip.");
  const count = view.getUint16(end + 10, true), entries = [];
  let at = view.getUint32(end + 16, true);
  for (let i = 0; i < count; i++) {
    if (view.getUint32(at, true) !== 0x02014b50) throw new Error("This file is not a readable zip.");
    const length = view.getUint16(at + 28, true);
    entries.push({
      flags: view.getUint16(at + 8, true), method: view.getUint16(at + 10, true), packed: view.getUint32(at + 20, true), size: view.getUint32(at + 24, true),
      local: view.getUint32(at + 42, true), name: decoder.decode(bytes.subarray(at + 46, at + 46 + length)),
      time: view.getUint32(at + 12, true),
    });
    at += 46 + length + view.getUint16(at + 30, true) + view.getUint16(at + 32, true);
  }
  return entries;
}

async function zipRead(bytes, entry) {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.length);
  const start = entry.local + 30 + view.getUint16(entry.local + 26, true) + view.getUint16(entry.local + 28, true);
  const packed = bytes.subarray(start, start + entry.packed);
  return entry.method === 8 ? inflate(packed) : packed;
}

function zipWrite(files) {
  const parts = [], central = [];
  let offset = 0;
  for (const { name, data, time } of files) {
    const title = encoder.encode(name), crc = crc32(data), local = new DataView(new ArrayBuffer(30));
    local.setUint32(0, 0x04034b50, true); local.setUint16(4, 20, true); local.setUint16(6, 0x800, true); local.setUint32(10, time, true);
    local.setUint32(14, crc, true); local.setUint32(18, data.length, true); local.setUint32(22, data.length, true); local.setUint16(26, title.length, true);
    const entry = new DataView(new ArrayBuffer(46));
    entry.setUint32(0, 0x02014b50, true); entry.setUint16(4, 20, true); entry.setUint16(6, 20, true); entry.setUint16(8, 0x800, true); entry.setUint32(12, time, true);
    entry.setUint32(16, crc, true); entry.setUint32(20, data.length, true); entry.setUint32(24, data.length, true); entry.setUint16(28, title.length, true); entry.setUint32(42, offset, true);
    parts.push(new Uint8Array(local.buffer), title, data);
    central.push(new Uint8Array(entry.buffer), title);
    offset += 30 + title.length + data.length;
  }
  const size = central.reduce((sum, part) => sum + part.length, 0), end = new DataView(new ArrayBuffer(22));
  end.setUint32(0, 0x06054b50, true); end.setUint16(8, files.length, true); end.setUint16(10, files.length, true); end.setUint32(12, size, true); end.setUint32(16, offset, true);
  const all = [...parts, ...central, new Uint8Array(end.buffer)];
  const out = new Uint8Array(all.reduce((sum, part) => sum + part.length, 0));
  let at = 0;
  for (const part of all) { out.set(part, at); at += part.length; }
  return out;
}

async function batch(bytes) {
  const summary = { compressed: 0, restored: 0, copied: 0, skipped: 0, problems: [] }, files = [], used = new Set();
  const entries = zipEntries(bytes).filter((e) => !e.name.endsWith("/") && !e.name.startsWith("__MACOSX/"));
  if (!entries.length) throw new Error("This zip has no files in it.");
  if (entries.length > 1000) throw new Error("A zip can hold up to 1,000 files.");
  const unique = (name) => {
    const dot = name.lastIndexOf(".") > name.lastIndexOf("/") ? name.lastIndexOf(".") : name.length;
    let candidate = name;
    for (let n = 2; used.has(candidate.toLowerCase()); n++) candidate = `${name.slice(0, dot)} (${n})${name.slice(dot)}`;
    used.add(candidate.toLowerCase());
    return candidate;
  };
  const note = (name) => { if (summary.problems.length < 20) summary.problems.push(name); };
  for (const entry of entries) {
    const flat = entry.name.replaceAll("\\", "/"), pieces = flat.split("/");
    if (flat.startsWith("/") || pieces.includes("..") || flat.includes(":") || entry.flags & 1 || ![0, 8].includes(entry.method)) {
      summary.skipped++; note(entry.name); continue;
    }
    const name = pieces.filter((p) => p && p !== ".").join("/");
    let data;
    try {
      data = await zipRead(bytes, entry);
      if (data.length !== entry.size) throw new Error("bad");
    } catch { summary.skipped++; note(name); continue; }
    const leaf = pieces[pieces.length - 1];
    if (/\.(flac|wav)$/i.test(name)) {
      try {
        const done = await decodeFile(data);
        const folder = name.slice(0, name.length - leaf.length);
        files.push({ name: unique(folder + done.result.name), data: done.bytes, time: entry.time });
        summary.restored++;
      } catch {
        note(name); files.push({ name: unique(name), data, time: entry.time }); summary.copied++;
      }
      continue;
    }
    const done = await encodeFile(data, leaf);
    if (done.audio.length < data.length) { files.push({ name: unique(`${name}.flac`), data: done.audio, time: entry.time }); summary.compressed++; }
    else { files.push({ name: unique(name), data, time: entry.time }); summary.copied++; }
  }
  const out = zipWrite(files);
  return { bytes: out, result: { ...summary, before: bytes.length, after: out.length } };
}

onmessage = async ({ data }) => {
  const { id, job, bytes, name } = data, input = new Uint8Array(bytes);
  try {
    if (job === "encode") {
      const { audio, result } = await encodeFile(input, name);
      const copy = audio.slice();
      postMessage({ id, bytes: copy.buffer, type: "audio/flac", result }, [copy.buffer]);
    } else if (job === "decode") {
      const { bytes: file, result } = await decodeFile(input);
      const copy = file.slice();
      postMessage({ id, bytes: copy.buffer, type: "application/octet-stream", result }, [copy.buffer]);
    } else {
      const { bytes: file, result } = await batch(input);
      postMessage({ id, bytes: file.buffer, type: "application/zip", result }, [file.buffer]);
    }
  } catch (error) {
    postMessage({ id, error: error.message });
  }
};
