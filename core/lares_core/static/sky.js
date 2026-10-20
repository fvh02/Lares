// Lares – the sky behind the design "Himmel".
//
// The colours follow the real position of the sun at the place of the device (computed from latitude, longitude and
// time), so sunrise and sunset happen exactly when they happen outside. Weather from Open-Meteo through Lares:
// clouds, rain, drizzle, snow, fog and thunderstorms; wind moves clouds and tilts the rain. In dark mode the whole
// sky is tinted dark. With "reduce motion" only a still picture is drawn.
"use strict";
(() => {
  const RAD = Math.PI / 180;
  const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
  const mix = (a, b, t) => a + (b - a) * t;
  const hex = (h) => [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16));
  const mixColor = (a, b, t) => a.map((v, i) => mix(v, b[i], t));
  const rgba = (c, a = 1) => `rgba(${c[0] | 0},${c[1] | 0},${c[2] | 0},${a})`;

  // -- the sun ------------------------------------------------------------------------------------
  // Position of the sun (elevation and azimuth in degrees) – accurate to a fraction of a degree, enough for the sky.
  function sunPosition(date, lat, lon) {
    const d = date.getTime() / 86400000 + 2440587.5 - 2451545.0;
    const g = ((357.529 + 0.98560028 * d) % 360) * RAD;
    const q = (280.459 + 0.98564736 * d) % 360;
    const L = (q + 1.915 * Math.sin(g) + 0.020 * Math.sin(2 * g)) * RAD;
    const e = (23.439 - 0.00000036 * d) * RAD;
    const ra = Math.atan2(Math.cos(e) * Math.sin(L), Math.cos(L));
    const dec = Math.asin(Math.sin(e) * Math.sin(L));
    const gmst = ((18.697374558 + 24.06570982441908 * d) % 24) * 15;
    const H = (gmst + lon) * RAD - ra;
    const phi = lat * RAD;
    const el = Math.asin(Math.sin(phi) * Math.sin(dec) + Math.cos(phi) * Math.cos(dec) * Math.cos(H));
    const az = Math.atan2(-Math.sin(H), Math.tan(dec) * Math.cos(phi) - Math.sin(phi) * Math.cos(H));
    return {el: el / RAD, az: (az / RAD + 360) % 360};
  }
  // Moon: age in the synodic month (0 new, 0.5 full) – for its shape.
  function moonPhase(date) {
    const days = date.getTime() / 86400000 - 10956.5 - 5.9;       // a known new moon: 6 Jan 2000
    return ((days % 29.530588) + 29.530588) % 29.530588 / 29.530588;
  }

  // Colours of the sky (top, horizon) by the elevation of the sun.
  const SKY = [
    [-18, "#03050c", "#070b18"],
    [-12, "#060a1c", "#111a36"],
    [-8, "#0e1638", "#2a2c58"],
    [-5, "#1c2552", "#6b4a72"],
    [-2, "#2d3d78", "#d0805e"],
    [0, "#3f5b9c", "#f39a5a"],
    [3, "#5277b8", "#f7b98a"],
    [7, "#4f86cf", "#f3d6b8"],
    [14, "#3f80d4", "#bcd9f3"],
    [35, "#2f74cf", "#9fc9ef"],
    [90, "#2a6cc8", "#94c2ec"],
  ].map(([el, top, hor]) => [el, hex(top), hex(hor)]);
  function skyColors(el) {
    for (let i = 1; i < SKY.length; i++) {
      if (el <= SKY[i][0]) {
        const [e0, t0, h0] = SKY[i - 1], [e1, t1, h1] = SKY[i];
        const t = clamp((el - e0) / (e1 - e0), 0, 1);
        return [mixColor(t0, t1, t), mixColor(h0, h1, t)];
      }
    }
    return el < SKY[0][0] ? [SKY[0][1], SKY[0][2]] : [SKY.at(-1)[1], SKY.at(-1)[2]];
  }
  // Clouds swallow colour: the sky turns grey – lighter by day, darker at night.
  function overcast(c, amount, el) {
    const lum = (0.3 * c[0] + 0.59 * c[1] + 0.11 * c[2]);
    const grey = el > 0 ? clamp(lum * 1.05 + 25, 90, 205) : lum * 0.9;
    return mixColor(c, [grey, grey, grey * 1.03], amount);
  }

  // -- weather codes (WMO) ------------------------------------------------------------------------
  function kind(w) {
    const c = w.code;
    return {
      fog: c === 45 || c === 48,
      drizzle: c >= 51 && c <= 57,
      rain: (c >= 61 && c <= 67) || (c >= 80 && c <= 82),
      snow: (c >= 71 && c <= 77) || c === 85 || c === 86,
      storm: c >= 95,
    };
  }
  const WORDS = {0: "Klar", 1: "Überwiegend klar", 2: "Teils bewölkt", 3: "Bedeckt", 45: "Nebel", 48: "Raureifnebel",
    51: "Leichter Nieselregen", 53: "Nieselregen", 55: "Starker Nieselregen", 56: "Gefrierender Niesel", 57: "Gefrierender Niesel",
    61: "Leichter Regen", 63: "Regen", 65: "Starker Regen", 66: "Gefrierender Regen", 67: "Gefrierender Regen",
    71: "Leichter Schneefall", 73: "Schneefall", 75: "Starker Schneefall", 77: "Schneegriesel",
    80: "Regenschauer", 81: "Regenschauer", 82: "Heftige Schauer", 85: "Schneeschauer", 86: "Schneeschauer",
    95: "Gewitter", 96: "Gewitter mit Hagel", 99: "Gewitter mit Hagel"};

  // -- the canvas ---------------------------------------------------------------------------------
  const canvas = document.createElement("canvas");
  canvas.id = "sky";
  canvas.setAttribute("aria-hidden", "true");
  const ctx = canvas.getContext("2d");
  const layer = document.createElement("canvas"), lctx = layer.getContext("2d");
  const still = matchMedia("(prefers-reduced-motion: reduce)");
  let W = 0, H = 0, DPR = 1, running = false, last = 0;
  let place = null, weather = {code: 2, wolken: 35, niederschlag: 0, wind: 8, windrichtung: 250};
  let override = null;            // for trying out: LaresSky.set({zeit, code, wolken, …})
  let stars = [], clouds = [], drops = [], flakes = [], flash = 0, nextFlash = 0;

  function resize() {
    DPR = Math.min(window.devicePixelRatio || 1, 2);
    W = innerWidth; H = innerHeight;
    canvas.width = layer.width = W * DPR; canvas.height = layer.height = H * DPR;
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    stars = Array.from({length: Math.round(W * H / 2600)}, () => ({
      x: Math.random() * W, y: Math.random() * H * 0.8, r: Math.random() * 1.1 + 0.25, p: Math.random() * 6.28,
      s: 0.6 + Math.random() * 1.6}));
    buildClouds();
    buildParticles();
  }

  // a cloud is a sprite of soft overlapping puffs, drawn once and moved
  function cloudSprite(w, h, dense) {
    const c = document.createElement("canvas");
    c.width = w * 2; c.height = h * 2.2;
    const g = c.getContext("2d");
    const puffs = 9 + Math.random() * 10;
    for (let i = 0; i < puffs; i++) {
      const px = w * (0.35 + Math.random() * 1.3), py = h * (0.85 + Math.random() * 0.6) - Math.abs(px - w) * 0.25;
      const r = h * (0.35 + Math.random() * 0.55) * (1 - Math.abs(px - w) / w * 0.45);
      const grad = g.createRadialGradient(px, py, r * 0.1, px, py, r);
      grad.addColorStop(0, `rgba(255,255,255,${dense ? 0.9 : 0.75})`);
      grad.addColorStop(0.55, `rgba(255,255,255,${dense ? 0.55 : 0.4})`);
      grad.addColorStop(1, "rgba(255,255,255,0)");
      g.fillStyle = grad;
      g.beginPath(); g.arc(px, py, r, 0, 6.29); g.fill();
    }
    // a darker underside, as on real clouds
    g.globalCompositeOperation = "source-atop";
    const under = g.createLinearGradient(0, h * 0.6, 0, h * 2);
    under.addColorStop(0, "rgba(0,0,0,0)");
    under.addColorStop(1, "rgba(60,70,90,0.35)");
    g.fillStyle = under; g.fillRect(0, 0, c.width, c.height);
    return c;
  }
  function buildClouds() {
    const w = state().weather;
    const cover = clamp(w.wolken / 100, 0, 1);
    const n = Math.round(cover * (W / 95) + (cover > 0.05 ? 2 : 0));
    clouds = Array.from({length: n}, (_, i) => {
      const depth = Math.random();
      const cw = mix(140, 420, depth) * (W < 600 ? 0.7 : 1);
      return {sprite: cloudSprite(cw, cw * 0.32, cover > 0.7), x: Math.random() * (W + cw * 2) - cw * 2,
        y: mix(0.04, 0.55, Math.pow(Math.random(), 1.4)) * H, w: cw, depth, a: mix(0.55, 1, cover) * mix(0.7, 1, depth), i};
    });
    clouds.sort((a, b) => a.depth - b.depth);
  }
  function buildParticles() {
    const w = state().weather, k = kind(w);
    const amount = Math.max(w.niederschlag, k.drizzle ? 0.3 : 0, k.rain ? 0.8 : 0, k.storm ? 3 : 0);
    const nRain = (k.rain || k.drizzle || k.storm) ? Math.round(clamp(amount, 0.2, 12) * W / 9 * (k.drizzle ? 0.6 : 1)) : 0;
    drops = Array.from({length: Math.min(nRain, 1400)}, () => ({x: Math.random() * W, y: Math.random() * H,
      l: k.drizzle ? 6 + Math.random() * 6 : 12 + Math.random() * 16, v: k.drizzle ? 380 + Math.random() * 120 : 750 + Math.random() * 350,
      a: 0.25 + Math.random() * 0.35}));
    const nSnow = k.snow ? Math.round(clamp(Math.max(w.schnee * 4, 0.6), 0.5, 6) * W / 7) : 0;
    flakes = Array.from({length: Math.min(nSnow, 900)}, () => ({x: Math.random() * W, y: Math.random() * H,
      r: 0.8 + Math.random() * 2.4, v: 25 + Math.random() * 45, p: Math.random() * 6.28}));
  }

  function state() {
    const now = override && override.zeit ? new Date(override.zeit) : new Date();
    const w = {...weather, ...(override || {})};
    const lat = (place && place.lat) ?? 51.2, lon = (place && place.lon) ?? 10.4;
    return {now, weather: w, sun: sunPosition(now, lat, lon), dark: document.documentElement.dataset.dark === "1"};
  }

  // -- drawing ------------------------------------------------------------------------------------
  function draw(dt, t) {
    const {now, weather: w, sun, dark} = state();
    const k = kind(w);
    const cover = clamp(w.wolken / 100, 0, 1);
    const grey = clamp(cover * 0.75 + (k.rain || k.drizzle || k.snow ? 0.25 : 0) + (k.storm ? 0.35 : 0) + (k.fog ? 0.4 : 0), 0, 0.92);
    let [top, hor] = skyColors(sun.el);
    top = overcast(top, grey, sun.el); hor = overcast(hor, grey * 0.85, sun.el);
    const horizonY = H * 0.86;

    // sky
    const sky = ctx.createLinearGradient(0, 0, 0, H);
    sky.addColorStop(0, rgba(top)); sky.addColorStop(horizonY / H, rgba(hor)); sky.addColorStop(1, rgba(mixColor(hor, top, 0.35)));
    ctx.fillStyle = sky; ctx.fillRect(0, 0, W, H);

    // where the sun stands: east on the left, west on the right (looking south)
    const sx = clamp((sun.az - 90) / 180, -0.15, 1.15) * W;
    const sy = horizonY - (sun.el / 70) * horizonY;
    const golden = clamp(1 - Math.abs(sun.el - 1) / 9, 0, 1) * (1 - grey * 0.8);

    // glow of sunrise and sunset along the horizon
    if (golden > 0.01) {
      const glow = ctx.createRadialGradient(sx, horizonY, 0, sx, horizonY, Math.max(W, H) * 0.9);
      glow.addColorStop(0, `rgba(255,170,90,${0.55 * golden})`);
      glow.addColorStop(0.35, `rgba(255,120,80,${0.22 * golden})`);
      glow.addColorStop(1, "rgba(255,120,80,0)");
      ctx.fillStyle = glow; ctx.fillRect(0, 0, W, H);
    }

    // twilight: the first and last light glows on the horizon where the sun is, before it rises
    const twilight = clamp(1 - Math.abs(sun.el + 5) / 7, 0, 1) * (1 - grey * 0.85);
    if (twilight > 0.01) {
      const tx = clamp(sx, W * 0.05, W * 0.95);
      const tw = ctx.createRadialGradient(tx, horizonY, 0, tx, horizonY, Math.max(W, H) * 0.75);
      tw.addColorStop(0, `rgba(240,140,110,${0.45 * twilight})`);
      tw.addColorStop(0.3, `rgba(170,100,150,${0.22 * twilight})`);
      tw.addColorStop(1, "rgba(120,90,160,0)");
      ctx.fillStyle = tw; ctx.fillRect(0, 0, W, H);
    }

    // stars, fading in at dusk and hidden by clouds
    const night = clamp((-sun.el - 4) / 10, 0, 1) * (1 - cover * 0.95);
    if (night > 0.01) {
      for (const s of stars) {
        const tw = 0.6 + 0.4 * Math.sin(t / 1000 * s.s + s.p);
        ctx.fillStyle = `rgba(255,255,255,${night * tw * (1 - s.y / H)})`;
        ctx.beginPath(); ctx.arc(s.x, s.y, s.r, 0, 6.29); ctx.fill();
      }
    }

    // moon at night: high in the sky, with its phase
    if (sun.el < -2) {
      const phase = moonPhase(now);
      const mx = W * 0.78, my = H * 0.17, mr = Math.min(W, H) * 0.032 + 6;
      const vis = clamp((-sun.el - 2) / 6, 0, 1) * (1 - cover * 0.85);
      if (vis > 0.02) {
        const halo = ctx.createRadialGradient(mx, my, mr, mx, my, mr * 6);
        halo.addColorStop(0, `rgba(220,230,255,${0.18 * vis})`); halo.addColorStop(1, "rgba(220,230,255,0)");
        ctx.fillStyle = halo; ctx.fillRect(mx - mr * 6, my - mr * 6, mr * 12, mr * 12);
        ctx.save();
        ctx.beginPath(); ctx.arc(mx, my, mr, 0, 6.29); ctx.clip();
        ctx.fillStyle = `rgba(235,238,245,${vis})`; ctx.fillRect(mx - mr, my - mr, mr * 2, mr * 2);
        // the unlit part: a disk in the colour of the sky, pushed aside as the moon grows
        const lit = (1 - Math.cos(phase * 2 * Math.PI)) / 2;       // 0 new moon, 1 full moon
        ctx.fillStyle = rgba(top, vis * 0.94);
        ctx.beginPath(); ctx.arc(mx + (phase < 0.5 ? -1 : 1) * 2 * mr * lit, my, mr * 1.02, 0, 6.29); ctx.fill();
        ctx.restore();
      }
    }

    // the sun itself
    if (sun.el > -3) {
      const vis = clamp((sun.el + 3) / 4, 0, 1) * (1 - grey * 0.9);
      const r = Math.min(W, H) * 0.035 + 8;
      const warm = clamp(1 - sun.el / 12, 0, 1);
      const core = mixColor([255, 252, 235], [255, 190, 110], warm);
      const halo = ctx.createRadialGradient(sx, sy, r * 0.5, sx, sy, r * 9);
      halo.addColorStop(0, rgba(core, 0.55 * vis)); halo.addColorStop(0.25, rgba(core, 0.16 * vis)); halo.addColorStop(1, rgba(core, 0));
      ctx.fillStyle = halo; ctx.fillRect(sx - r * 9, sy - r * 9, r * 18, r * 18);
      ctx.fillStyle = rgba(core, vis);
      ctx.beginPath(); ctx.arc(sx, sy, r, 0, 6.29); ctx.fill();
    }

    // clouds, lit by the sky: white by day, peach at sunrise and sunset, slate at night
    const day = clamp((sun.el + 4) / 14, 0, 1);
    const light = mixColor(mixColor([70, 78, 100], [255, 196, 160], golden), [255, 255, 255], clamp(day - golden * 0.6, 0, 1));
    const shade = mixColor(light, [120, 128, 140], grey * 0.55);
    const wind = (w.wind || 5) * (Math.sin(w.windrichtung * RAD) >= 0 ? -1 : 1);   // wind from the west moves east
    if (clouds.length) {
      // the white sprites go onto their own layer, take the light of the hour there, then onto the sky
      lctx.setTransform(DPR, 0, 0, DPR, 0, 0);
      lctx.globalCompositeOperation = "source-over";
      lctx.clearRect(0, 0, W, H);
      for (const c of clouds) {
        c.x += wind * mix(0.6, 1.6, c.depth) * dt / 1000 * 1.6;
        if (c.x > W + 40) c.x = -c.w * 2; else if (c.x < -c.w * 2 - 40) c.x = W;
        lctx.globalAlpha = c.a;
        lctx.drawImage(c.sprite, c.x, c.y, c.w * 2, c.w * 0.32 * 2.2);
      }
      lctx.globalAlpha = 1;
      lctx.globalCompositeOperation = "source-atop";
      lctx.fillStyle = rgba(shade, 0.9);
      lctx.fillRect(0, 0, W, H);
      ctx.globalAlpha = mix(0.55, 1, day * 0.6 + 0.4);
      ctx.drawImage(layer, 0, 0, W, H);
      ctx.globalAlpha = 1;
    }
    if (cover > 0.85) {      // closed cloud deck
      const deck = ctx.createLinearGradient(0, 0, 0, H * 0.7);
      deck.addColorStop(0, rgba(mixColor(shade, top, 0.3), 0.55 * (cover - 0.85) / 0.15));
      deck.addColorStop(1, rgba(shade, 0));
      ctx.fillStyle = deck; ctx.fillRect(0, 0, W, H * 0.7);
    }

    // rain and drizzle, tilted by the wind
    if (drops.length) {
      const tilt = clamp(wind / 40, -0.5, 0.5);
      ctx.strokeStyle = day > 0.3 ? "rgba(225,232,245,1)" : "rgba(170,185,210,1)";
      ctx.lineWidth = 1;
      for (const d of drops) {
        d.y += d.v * dt / 1000; d.x += d.v * tilt * dt / 1000;
        if (d.y > H) { d.y = -d.l; d.x = Math.random() * W; }
        if (d.x > W) d.x -= W; else if (d.x < 0) d.x += W;
        ctx.globalAlpha = d.a;
        ctx.beginPath(); ctx.moveTo(d.x, d.y); ctx.lineTo(d.x - d.l * tilt, d.y - d.l); ctx.stroke();
      }
      ctx.globalAlpha = 1;
    }

    // snow
    for (const f of flakes) {
      f.y += f.v * dt / 1000; f.p += dt / 1000; f.x += (Math.sin(f.p) * 14 + wind * 0.8) * dt / 1000;
      if (f.y > H) { f.y = -4; f.x = Math.random() * W; }
      if (f.x > W) f.x -= W; else if (f.x < 0) f.x += W;
      ctx.fillStyle = `rgba(255,255,255,${0.55 + f.r / 8})`;
      ctx.beginPath(); ctx.arc(f.x, f.y, f.r, 0, 6.29); ctx.fill();
    }

    // fog: a bright haze from the ground up
    if (k.fog) {
      const fog = ctx.createLinearGradient(0, H * 0.25, 0, H);
      const fc = mixColor(hor, [210, 214, 220], day * 0.6);
      fog.addColorStop(0, rgba(fc, 0)); fog.addColorStop(0.6, rgba(fc, 0.55)); fog.addColorStop(1, rgba(fc, 0.8));
      ctx.fillStyle = fog; ctx.fillRect(0, 0, W, H);
    }

    // lightning now and then
    if (k.storm && !still.matches) {
      if (t > nextFlash) { flash = 1; nextFlash = t + 3500 + Math.random() * 9000; }
      if (flash > 0) {
        ctx.fillStyle = `rgba(235,240,255,${flash * 0.55})`; ctx.fillRect(0, 0, W, H);
        flash = Math.max(0, flash - dt / 140);
      }
    }

    // tell the page how bright the sky is, so text standing right on it stays readable
    const lum = 0.3 * (top[0] + hor[0]) / 2 + 0.59 * (top[1] + hor[1]) / 2 + 0.11 * (top[2] + hor[2]) / 2;
    const tone = lum < 118 ? "dunkel" : "hell";
    if (document.documentElement.dataset.sky !== tone) document.documentElement.dataset.sky = tone;

    // dark mode: the whole sky is tinted dark so the glass stays readable
    if (dark) { ctx.fillStyle = "rgba(6,8,14,0.52)"; ctx.fillRect(0, 0, W, H); }
  }

  function frame(t) {
    if (!running) return;
    const dt = Math.min(t - (last || t), 100);
    if (t - last >= 30 || !last) { draw(dt || 16, t); last = t; }       // about 30 frames per second is plenty
    requestAnimationFrame(frame);
  }
  function start() {
    if (running) return;
    running = true;
    if (still.matches) { running = false; draw(0, performance.now()); return; }
    last = 0; requestAnimationFrame(frame);
  }
  function stop() { running = false; }

  // -- place and weather ----------------------------------------------------------------------------
  async function loadWeather() {
    const q = place && place.geraet ? `?lat=${place.lat}&lon=${place.lon}` : "";
    try {
      const res = await fetch("/atrium/wetter" + q, {credentials: "same-origin"});
      if (!res.ok) return;
      const data = await res.json();
      weather = data;
      if (!place) place = {lat: data.ort.lat, lon: data.ort.lon};
      buildClouds(); buildParticles();
      document.dispatchEvent(new CustomEvent("lares-wetter", {detail: describe()}));
    } catch { /* offline: keep the last weather */ }
  }
  function locate() {
    if (!navigator.geolocation || !window.isSecureContext) return loadWeather();
    navigator.geolocation.getCurrentPosition(
      (p) => { place = {lat: +p.coords.latitude.toFixed(3), lon: +p.coords.longitude.toFixed(3), geraet: true}; loadWeather(); },
      () => loadWeather(), {maximumAge: 30 * 60000, timeout: 8000});
  }
  function describe() {
    const w = state().weather;
    return {text: WORDS[w.code] || "", temperatur: w.temperatur, quelle: w.quelle};
  }

  // -- public -------------------------------------------------------------------------------------
  let active = false, timer = null;
  window.LaresSky = {
    on() {
      if (active) return;
      active = true;
      document.body.prepend(canvas);
      resize(); start(); locate();
      timer = setInterval(locate, 15 * 60000);
    },
    off() {
      if (!active) return;
      active = false; stop(); clearInterval(timer); canvas.remove();
    },
    redraw() { if (active && !running) draw(0, performance.now()); },
    // for trying out: LaresSky.set({zeit: "2026-10-20T06:40", code: 61, wolken: 80, niederschlag: 2})
    set(values) { override = values ? {...(override || {}), ...values} : null; buildClouds(); buildParticles(); this.redraw(); },
    sunPosition, describe,
  };
  addEventListener("resize", () => { if (active) resize(); });
  document.addEventListener("visibilitychange", () => { if (!active) return; if (document.hidden) stop(); else start(); });
  still.addEventListener("change", () => { if (active) { stop(); start(); } });
})();
