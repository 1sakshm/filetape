"use strict";

// Everything that moves on the page. The tool itself lives in app.js and works without this file.
(() => {
  const C = { walnut: "#120f0d", bark: "#3a332d", cream: "#f4ecdf", plate: "#f4ecdf", amber: "#ff4a12", orange: "#a89c8d", red: "#ff4a12", olive: "#6e6459", ink: "#120f0d" };
  const MONO = '"Big Shoulders Display", "Arial Narrow", sans-serif';
  const LABEL = `800 16px ${MONO}`;
  const $ = (id) => document.getElementById(id);
  const clamp = (v, a = 0, b = 1) => Math.min(b, Math.max(a, v));
  const ease = (t) => t * t * (3 - 2 * t);
  const seg = (p, a, b) => clamp((p - a) / (b - a));
  const lerp = (a, b, t) => a + (b - a) * t;
  const noise = (n) => { const s = Math.sin(n * 127.1 + 11.7) * 43758.5453; return s - Math.floor(s); };
  const still = matchMedia("(prefers-reduced-motion: reduce)").matches;

  function fit(canvas) {
    const box = canvas.getBoundingClientRect(), d = Math.min(devicePixelRatio || 1, 2);
    const w = Math.round(box.width * d), h = Math.round(box.height * d);
    if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
    const g = canvas.getContext("2d");
    g.setTransform(d, 0, 0, d, 0, 0);
    return { g, W: box.width, H: box.height };
  }
  const say = (g, text, x, y, colour, align = "left", font = LABEL) => { g.font = font; g.fillStyle = colour; g.textAlign = align; g.fillText(text, x, y); };

  // A picture for the diagrams: a low sun over rolling hills, in the cabinet's colours.
  const PALETTE = ["#ffc4a8", "#ff8a5c", "#d93a0c", "#8c2506", "#3d1205", "#f4ecdf"];
  function picture(cols, rows) {
    const cells = [];
    for (let y = 0; y < rows; y++) for (let x = 0; x < cols; x++) {
      const u = (x + 0.5) / cols, v = (y + 0.5) / rows, ring = Math.hypot(u - 0.5, (v - 0.62) * 1.1);
      const hill = 0.68 + 0.07 * Math.sin(u * 7 + 1) + 0.03 * Math.sin(u * 19);
      let colour = ring < 0.2 ? 5 : ring < 0.3 ? 0 : ring < 0.42 ? 1 : ring < 0.56 ? 2 : 4;
      if (v > hill) colour = v > hill + 0.14 ? 4 : 3;
      cells.push(PALETTE[colour]);
    }
    return cells;
  }

  // The level both meters show: real audio while it plays, and tape noise otherwise.
  const levels = { now: 0.3, data: null };
  function level(t, running) {
    const analyser = window.filetapeAnalyser;
    if (analyser) {
      levels.data ??= new Uint8Array(analyser.fftSize);
      if (levels.data.length !== analyser.fftSize) levels.data = new Uint8Array(analyser.fftSize);
      analyser.getByteTimeDomainData(levels.data);
      let sum = 0;
      for (const v of levels.data) sum += (v - 128) ** 2;
      return clamp(Math.sqrt(sum / levels.data.length) / 28);
    }
    return running ? 0.55 + 0.2 * Math.sin(t * 9) * Math.sin(t * 3.7) + 0.12 * noise(Math.floor(t * 14)) : 0.05;
  }

  function meter(g, x, y, w, value) {
    const h = w * 0.62;
    g.fillStyle = C.ink; g.fillRect(x, y, w, h);
    const cx = x + w / 2, cy = y + h * 1.05, r = h * 0.8;
    g.lineWidth = 2;
    g.strokeStyle = C.cream; g.beginPath(); g.arc(cx, cy, r, -2.25, -1.25); g.stroke();
    g.strokeStyle = C.amber; g.beginPath(); g.arc(cx, cy, r, -1.25, -0.89); g.stroke();
    say(g, "VU", cx, y + h * 0.62, "#a89c8d", "center");
    const angle = lerp(-2.25, -0.89, value);
    g.strokeStyle = C.cream; g.lineWidth = 1.5; g.beginPath(); g.moveTo(cx, y + h); g.lineTo(cx + Math.cos(angle) * r * 1.02, cy + Math.sin(angle) * r * 1.02); g.stroke();
  }

  function reel(g, x, y, r, hub, spin) {
    g.fillStyle = C.ink; g.beginPath(); g.arc(x, y, r, 0, 7); g.fill();
    g.strokeStyle = "#3a332d"; g.lineWidth = 1;
    for (let i = hub + 4; i < r; i += 5) { g.beginPath(); g.arc(x, y, i, 0, 7); g.stroke(); }
    g.fillStyle = C.cream; g.beginPath(); g.arc(x, y, hub, 0, 7); g.fill();
    g.fillStyle = C.ink;
    for (let k = 0; k < 3; k++) {
      const a = spin + k * 2.094;
      g.beginPath(); g.arc(x + Math.cos(a) * hub * 0.58, y + Math.sin(a) * hub * 0.58, hub * 0.27, 0, 7); g.fill();
    }
    g.beginPath(); g.arc(x, y, hub * 0.14, 0, 7); g.fill();
  }

  // The hero loop: the image records onto tape, then plays back into the same picture.
  const tape = (() => {
    const canvas = $("tape"), COLS = 34, ROWS = 28, CYCLE = 16, cells = picture(COLS, ROWS);
    let boost = 0, clock = 0, last = 0;
    canvas.addEventListener("pointerdown", () => { boost = 1; });
    return (t) => {
      const { g, W, H } = fit(canvas);
      g.clearRect(0, 0, W, H);
      clock += clamp(t - last, 0, 0.05) * (1 + boost * 3);
      last = t;
      boost = Math.max(0, boost - 0.012);
      const c = still ? 6 : clock % CYCLE;
      const f = still ? 1 : c < 6 ? ease(c / 6) : c < 8 ? 1 : c < 14 ? ease(1 - (c - 8) / 6) : 0;
      const recording = c < 8, moving = !still && (c < 6 || (c >= 8 && c < 14));
      const wide = W > 760;
      const s = Math.min(H * 0.8, W * (wide ? 0.3 : 0.4)), cell = s / COLS, cellY = s / ROWS;
      const ix = 14, iy = 14;
      const rmax = Math.min(H * 0.36, W * (wide ? 0.1 : 0.13)), rmin = rmax * 0.42, hub = rmin * 0.82;
      const left = { x: ix + s + rmax + (wide ? 34 : 10), y: H * 0.44 };
      const right = { x: W - rmax - 14, y: H * 0.44 };
      const y = H - 24, hx = left.x, end = right.x;
      const rl = lerp(rmax, rmin, f), rr = lerp(rmin, rmax, f);

      // Waveform on the tape, growing from the head towards the take-up reel.
      const span = end - hx, front = hx + span * f, step = 6;
      g.fillStyle = C.ink; g.fillRect(hx - rl, y, end + rr - hx + rl, 2);
      const flow = moving ? (recording ? t : -t) * 90 : 0;
      for (let x = hx; x < front; x += step) {
        const k = Math.floor((x - flow) / step), a = noise(k) * 0.8 + 0.2;
        g.fillStyle = k % 7 === 0 ? C.cream : C.ink;
        const bar = a * Math.min(70, H * 0.2);
        g.fillRect(x, y - bar - 2, step - 2, bar);
      }

      const spin = moving ? t * (recording ? 5 : -5) * (1 + boost * 3) : t * 0.2;
      g.strokeStyle = C.ink; g.lineWidth = 1.5;
      g.beginPath(); g.moveTo(left.x - rl, left.y); g.lineTo(left.x - rl, y + 3); g.moveTo(right.x + rr, right.y); g.lineTo(right.x + rr, y + 3); g.stroke();
      reel(g, left.x, left.y, rl, hub, spin);
      reel(g, right.x, right.y, rr, hub, spin * 0.8);

      // Pixels leave column by column, and return the same way.
      const front2 = f * (COLS + 8);
      for (let r = 0; r < ROWS; r++) for (let q = 0; q < COLS; q++) {
        const x0 = ix + q * cell, y0 = iy + r * cellY, delay = noise(q * 31 + r * 17) * 5;
        const u = clamp((front2 - q - delay) / 3);
        if (u >= 1) continue;
        g.fillStyle = cells[r * COLS + q];
        if (u <= 0) { g.fillRect(x0, y0, cell + 0.6, cellY + 0.6); continue; }
        const e = ease(u), lift = Math.sin(e * Math.PI) * H * 0.3;
        g.fillRect(lerp(x0, hx, e), lerp(y0, y, e) - lift, cell * (1 - e * 0.5), cellY * (1 - e * 0.5));
      }

      // Meters and the mode lamp sit between the reels.
      const roomLeft = left.x + rmax + 10, room = right.x - rmax - 10 - roomLeft;
      levels.now += (level(t, moving) - levels.now) * 0.3;
      if (room > 170) {
        const mw = Math.min(150, (room - 12) / 2), my = Math.max(6, left.y - mw * 0.5);
        meter(g, roomLeft + room / 2 - mw - 6, my, mw, levels.now);
        meter(g, roomLeft + room / 2 + 6, my, mw, clamp(levels.now * (0.9 + 0.1 * Math.sin(t * 13))));
        const lamp = C.cream;
        g.fillStyle = moving && recording && Math.sin(t * 8) > -0.4 ? lamp : C.ink;
        g.beginPath(); g.arc(roomLeft + room / 2 - 76, my + mw * 0.78, 7, 0, 7); g.fill();
        say(g, recording ? "RECORD: FILE TO SOUND" : "PLAY: SOUND TO FILE", roomLeft + room / 2 - 62, my + mw * 0.78 + 5, C.ink);
      }
    };
  })();

  // The scroll diagram.
  const method = (() => {
    const canvas = $("method"), section = $("how"), speech = $("method-say"), dots = [...$("method-steps").children];
    const COLS = 24, ROWS = 18, cells = picture(COLS, ROWS), STEPS = 4;
    const RUNNERS = [["COMPRESSED", 0.62, C.amber], ["FILE AS IT IS", 1, C.cream]];
    const hex = (n) => Math.floor(noise(n) * 256).toString(16).padStart(2, "0").toUpperCase();

    function read(g, a, q, t) {
      const s = Math.min(a.w / COLS, a.h * 0.8 / ROWS), ox = a.x + (a.w - s * COLS) / 2, oy = a.y + (a.h - s * ROWS) / 2;
      const line = Math.floor(ease(q) * ROWS);
      for (let r = 0; r < ROWS; r++) for (let c = 0; c < COLS; c++) {
        g.globalAlpha = r <= line ? 1 : 0.2;
        g.fillStyle = cells[r * COLS + c];
        g.fillRect(ox + c * s, oy + r * s, s - 1, s - 1);
      }
      g.globalAlpha = 1;
      g.fillStyle = C.cream; g.fillRect(ox - 8, oy + (line + 0.5) * s - 1, s * COLS + 16, 3);
      say(g, "SCANNING", ox, oy - 10, C.amber);
    }

    function race(g, a, q, t) {
      const gap = Math.min(70, (a.h - 60) / 3), top = a.y + (a.h - gap * 3) / 2;
      say(g, "SHORTER WINS", a.x, top - 14, C.amber);
      RUNNERS.forEach(([name, size, colour], i) => {
        const y = top + i * gap, grow = ease(seg(q, 0.05 + i * 0.1, 0.6 + i * 0.1));
        const wobble = grow < 1 ? Math.sin(t * 14 + i * 2) * 5 : 0;
        const w = a.w * 0.82 * size * grow + wobble;
        say(g, name, a.x, y + 12, C.cream);
        g.fillStyle = colour; g.fillRect(a.x, y + 18, Math.max(0, w), gap * 0.46);
      });
      const kept = ease(seg(q, 0.72, 0.9));
      if (kept > 0) {
        const y = top + 18, x = a.x + a.w * 0.82 * 0.62 + 12;
        g.save(); g.translate(x + 26, y + gap * 0.23); g.globalAlpha *= kept;
        say(g, "KEPT", 0, 4, C.amber, "center", `800 20px ${MONO}`);
        g.restore();
      }
    }

    function samples(g, a, q, t) {
      const rows = 4, cols = Math.floor(a.w / 44), lit = Math.floor(ease(q) * rows * cols);
      const graphY = a.y + a.h * 0.72, h = a.h * 0.24;
      g.font = `800 17px ${MONO}`; g.textAlign = "left";
      g.strokeStyle = "rgba(243,230,200,.3)"; g.lineWidth = 1; g.beginPath(); g.moveTo(a.x, graphY); g.lineTo(a.x + a.w, graphY); g.stroke();
      say(g, "TWO BYTES, ONE 16-BIT SAMPLE", a.x, a.y + 12, C.amber);
      g.strokeStyle = C.amber; g.lineWidth = 2; g.beginPath();
      for (let i = 0; i < rows * cols; i++) {
        const x = a.x + (i % cols) * 44, y = a.y + 32 + Math.floor(i / cols) * 28, n = i * 2;
        const on = i < lit, current = i === lit;
        if (on || current) { g.fillStyle = current ? C.red : C.amber; g.fillRect(x - 4, y - 3, 40, 22); }
        say(g, hex(n) + hex(n + 1), x, y + 13, on ? C.ink : C.cream, "left", `800 16px ${MONO}`);
      }
      for (let i = 0; i < lit; i++) {
        const px = a.x + i / (rows * cols) * a.w, py = graphY + (noise(i * 2) + noise(i * 2 + 1) - 1) * h;
        if (i) g.lineTo(px, py); else g.moveTo(px, py);
      }
      g.stroke();
    }

    function press(g, a, q, t) {
      const size = Math.min(a.h * 0.72, a.w * 0.56), cx = a.x + a.w * 0.5, cy = a.y + a.h * 0.46;
      const r = size / 2 * ease(seg(q, 0, 0.45));
      g.fillStyle = "#231d18"; g.beginPath(); g.arc(cx, cy, r, 0, 7); g.fill();
      g.strokeStyle = C.bark; g.lineWidth = 1;
      for (let i = 10; i < r; i += 6) { g.beginPath(); g.arc(cx, cy, i, 0, 7); g.stroke(); }
      const label = r * 0.42, spin = t * 1.4;
      if (label > 2) {
        g.save(); g.translate(cx, cy); g.rotate(spin);
        g.fillStyle = C.amber; g.beginPath(); g.arc(0, 0, label, 0, 7); g.fill();
        g.fillStyle = C.ink; g.beginPath(); g.arc(0, 0, label * 0.14, 0, 7); g.fill();
        g.restore();
      }
      const size2 = ease(seg(q, 0.55, 0.75));
      if (size2 > 0) {
        g.save(); g.translate(cx, cy + size / 2 + 26); g.globalAlpha *= size2;
        say(g, "+0.2% CARRIER", 0, 0, C.cream, "center", `800 17px ${MONO}`);
        g.restore();
      }
      const stamp = ease(seg(q, 0.72, 0.92));
      if (stamp > 0) {
        g.save(); g.translate(cx, cy); g.globalAlpha *= stamp;
        const w = Math.min(a.w * 0.8, 240);
        g.fillStyle = C.ink; g.fillRect(-w / 2, -22, w, 44);
        say(g, "EVERY BYTE CHECKED", 0, 5, C.cream, "center", `800 22px ${MONO}`);
        g.restore();
      }
    }

    const draws = [read, race, samples, press];
    return (t) => {
      const { g, W, H } = fit(canvas);
      const box = section.getBoundingClientRect(), p = clamp(-box.top / (box.height - innerHeight));
      g.fillStyle = C.ink; g.fillRect(0, 0, W, H);
      const wide = W >= 820;
      const a = wide ? { x: W * 0.4, y: 44, w: W * 0.6 - 40, h: H - 88 } : { x: 16, y: H * 0.42, w: W - 32, h: H * 0.58 - 18 };
      const index = Math.min(STEPS - 1, Math.floor(p * STEPS));
      [...speech.children].forEach((node, i) => node.classList.toggle("on", i === index));
      dots.forEach((d, i) => d.classList.toggle("on", i <= index));
      for (let i = 0; i < STEPS; i++) {
        const a0 = i / STEPS, a1 = (i + 1) / STEPS, fade = 0.014;
        const alpha = (i === 0 ? 1 : seg(p, a0 - fade, a0 + fade)) * (i === STEPS - 1 ? 1 : 1 - seg(p, a1 - fade, a1 + fade));
        if (alpha <= 0) continue;
        g.save(); g.globalAlpha = alpha;
        draws[i](g, a, seg(p, a0, a1 - 0.02), t);
        g.restore();
      }
    };
  })();

  // Files slide into a cassette.
  const cassette = (() => {
    const canvas = $("cassette");
    return (t) => {
      const { g, W, H } = fit(canvas);
      g.clearRect(0, 0, W, H);
      const w = W * 0.9, h = w * 0.6, x = (W - w) / 2, y = H - h - 6;
      const files = 7, travel = H * 0.34;
      for (let i = 0; i < files; i++) {
        const u = still ? 0.4 : (t * 0.22 + i / files) % 1;
        const fx = x + w * (0.12 + 0.76 * noise(i * 3)), fy = lerp(-30, y + 30, ease(u)), s = 1 - ease(seg(u, 0.75, 1)) * 0.5;
        g.fillStyle = i % 4 === 0 ? C.amber : C.cream;
        g.fillRect(fx, fy, 30 * s, 38 * s);
        g.fillStyle = C.ink; g.fillRect(fx + 5 * s, fy + 12 * s, 20 * s, 3 * s); g.fillRect(fx + 5 * s, fy + 20 * s, 14 * s, 3 * s);
      }
      void travel;
      g.fillStyle = C.bark; g.fillRect(x, y, w, h);
      g.fillStyle = C.plate; g.fillRect(x + w * 0.06, y + h * 0.08, w * 0.88, h * 0.62);
      g.fillStyle = C.amber; g.fillRect(x + w * 0.06, y + h * 0.16, w * 0.88, 3);
      g.fillStyle = C.ink; g.fillRect(x + w * 0.2, y + h * 0.34, w * 0.6, h * 0.3);
      const r = h * 0.11, spin = still ? 0 : t * 3;
      reel(g, x + w * 0.32, y + h * 0.49, r, r * 0.9, spin);
      reel(g, x + w * 0.68, y + h * 0.49, r, r * 0.9, spin);
      say(g, "SIDE A: YOUR WHOLE FOLDER", x + w / 2, y + h * 0.84, C.cream, "center", `800 17px ${MONO}`);
    };
  })();

  // A live spectrum over the waveform while the audio plays.
  const scope = (() => {
    const canvas = $("scope");
    let bins = null;
    return () => {
      const analyser = window.filetapeAnalyser;
      const { g, W, H } = fit(canvas);
      g.clearRect(0, 0, W, H);
      if (!analyser || !W) return;
      bins ??= new Uint8Array(analyser.frequencyBinCount);
      analyser.getByteFrequencyData(bins);
      const n = 48, bw = W / n;
      g.fillStyle = "rgba(21,20,18,.78)"; g.fillRect(0, 0, W, H);
      for (let i = 0; i < n; i++) {
        const v = bins[Math.floor(i / n * bins.length * 0.8)] / 255, h = v * H * 0.92;
        for (let y = 0; y < h; y += 8) {
          g.fillStyle = y > H * 0.7 ? C.amber : C.cream;
          g.fillRect(i * bw + 1, H - y - 6, bw - 2, 6);
        }
      }
    };
  })();

  // Things that enter once, starting visible so the page is whole without this.
  document.querySelectorAll("[data-from]").forEach((bar) => {
    bar.style.left = `${bar.dataset.from}%`;
    bar.style.width = `${bar.dataset.to - bar.dataset.from}%`;
  });
  const seen = new IntersectionObserver((entries) => entries.forEach((entry) => {
    if (!entry.isIntersecting) return;
    entry.target.classList.remove("armed");
    seen.unobserve(entry.target);
  }), { threshold: 0.2 });
  if (!still) document.querySelectorAll("[data-arm]").forEach((node) => {
    if (node.getBoundingClientRect().top > innerHeight) node.classList.add("armed");
    seen.observe(node);
  });

  const visible = new Map();
  const watch = new IntersectionObserver((entries) => entries.forEach((e) => visible.set(e.target, e.isIntersecting)), { rootMargin: "100px" });
  const parts = [[$("tape"), tape], [$("how"), method], [$("cassette"), cassette], [$("deck"), scope]];
  parts.forEach(([node]) => watch.observe(node));
  function frame(now) {
    const t = now / 1000;
    parts.forEach(([node, draw]) => { if (visible.get(node)) draw(t); });
    requestAnimationFrame(frame);
  }
  parts.forEach(([, draw]) => draw(0));
  requestAnimationFrame(frame);
})();
