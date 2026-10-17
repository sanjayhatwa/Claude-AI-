/*
 * Viral Studio web app.
 *
 * Plain JavaScript, no build step, no third-party code, no requests to anything
 * but this local server. All DOM is built with createElement/textContent (the
 * h() helper); nothing user- or model-written is ever parsed as HTML. Markdown
 * reports are rendered by a small renderer that also builds DOM nodes and only
 * lets through http(s)/mailto links and images from the project's own files.
 */
(function () {
  "use strict";

  // ------------------------------------------------------------------ DOM helpers

  function h(tag, attrs) {
    const el = document.createElement(tag);
    if (attrs) {
      for (const key of Object.keys(attrs)) {
        const v = attrs[key];
        if (v === null || v === undefined || v === false) continue;
        if (key === "class") el.className = v;
        else if (key === "text") el.textContent = v;
        else if (key === "style") Object.assign(el.style, v);          // CSSOM only (CSP-safe)
        else if (key === "dataset") Object.assign(el.dataset, v);
        else if (key === "value" && (tag === "input" || tag === "textarea" || tag === "select")) el.value = v;
        else if (key.startsWith("on") && typeof v === "function") el.addEventListener(key.slice(2), v);
        else if (v === true) el.setAttribute(key, "");
        else el.setAttribute(key, String(v));
      }
    }
    for (let i = 2; i < arguments.length; i++) add(el, arguments[i]);
    return el;
  }

  function add(el, kid) {
    if (kid === null || kid === undefined || kid === false || kid === true) return;
    if (Array.isArray(kid)) { kid.forEach(k => add(el, k)); return; }
    el.appendChild(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }

  function clear(el) { el.replaceChildren(); return el; }
  const $ = sel => document.querySelector(sel);

  const SVGNS = "http://www.w3.org/2000/svg";
  // Simple stroke icons on a 24x24 grid (drawn for this app).
  const ICONS = {
    folder: "M3 7.5A2.5 2.5 0 0 1 5.5 5H9l2 2h7.5A2.5 2.5 0 0 1 21 9.5v8a2.5 2.5 0 0 1-2.5 2.5h-13A2.5 2.5 0 0 1 3 17.5z",
    sparkles: "M11 3l1.9 5.1L18 10l-5.1 1.9L11 17l-1.9-5.1L4 10l5.1-1.9z M18.5 14.5l.8 2.2 2.2.8-2.2.8-.8 2.2-.8-2.2-2.2-.8 2.2-.8z",
    scissors: "M6 9a3 3 0 1 0 0-6 3 3 0 0 0 0 6z M6 21a3 3 0 1 0 0-6 3 3 0 0 0 0 6z M8.6 7.6L20 19 M8.6 16.4L20 5",
    chart: "M4 20V11 M9.5 20V5 M15 20v-8 M20.5 20V8 M2.5 20.5h19",
    scan: "M3 8V5.5A2.5 2.5 0 0 1 5.5 3H8 M16 3h2.5A2.5 2.5 0 0 1 21 5.5V8 M21 16v2.5a2.5 2.5 0 0 1-2.5 2.5H16 M8 21H5.5A2.5 2.5 0 0 1 3 18.5V16 M8 8h8 M12 8v9",
    pen: "M4 20h4L19 9l-4-4L4 16z M13.5 6.5l4 4",
    pulse: "M3 12h4l3-8 4 16 3-8h4",
    layers: "M4 6h9 M8 12h12 M4 18h12",
    grid: "M4 4h7v7H4z M13 4h7v7h-7z M4 13h7v7H4z M13 13h7v7h-7z",
    sliders: "M4 6h9 M17 6h3 M4 12h3 M11 12h9 M4 18h11 M19 18h1 M15 4v4 M9 10v4 M17 16v4",
    plus: "M12 5v14 M5 12h14",
    x: "M6 6l12 12 M18 6L6 18",
    upload: "M12 16V4 M7 9l5-5 5 5 M4 20h16",
    link: "M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1 M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1",
    film: "M4 4h16v16H4z M8 4v16 M16 4v16 M4 9h4 M4 15h4 M16 9h4 M16 15h4",
    image: "M4 5h16v14H4z M4 16l5-5 4 4 3-3 4 4 M15.5 9h.01",
    music: "M9 18V6l11-2v12 M9 18a3 3 0 1 1-6 0 3 3 0 0 1 6 0z M20 16a3 3 0 1 1-6 0 3 3 0 0 1 6 0z",
    file: "M6 3h8l5 5v13H6z M14 3v5h5",
    check: "M5 12.5l4.5 4.5L19 7.5",
    warn: "M12 3.5l9.5 17h-19z M12 10v4.5 M12 17.5h.01",
    xcircle: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z M9 9l6 6 M15 9l-6 6",
    info: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z M12 11v6 M12 7.5h.01",
    download: "M12 4v12 M7 11l5 5 5-5 M4 20h16",
    play: "M7 4.5v15l12-7.5z",
    stop: "M6 6h12v12H6z",
    refresh: "M20 11a8 8 0 1 0-2.3 5.7 M20 4v7h-7",
    send: "M4 12l16-8-6 16-2.5-6.5z M11.5 13.5L20 4",
    copy: "M8 8h12v12H8z M4 16V4h12",
    trash: "M4 7h16 M9 7V4h6v3 M6 7l1 13h10l1-13",
    external: "M14 4h6v6 M20 4l-9 9 M18 14v6H4V6h6",
    text: "M5 7V5h14v2 M12 5v14 M9 19h6",
    hash: "M5 9h14 M5 15h14 M10 4L8 20 M16 4l-2 16",
    shape: "M4 4h8v8H4z M16.5 13a3.5 3.5 0 1 1 0 7 3.5 3.5 0 0 1 0-7z",
    bar: "M3 10h18v4H3z M3 10h9v4H3z",
    cc: "M3 5h18v14H3z M10.5 10.2a2 2 0 1 0 0 3.6 M17 10.2a2 2 0 1 0 0 3.6",
    wave: "M4 10v4 M8 7v10 M12 4v16 M16 8v8 M20 11v2",
    clock: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z M12 7v5l3 2",
    save: "M5 4h11l3 3v13H5z M8 4v5h7V4 M8 20v-6h8v6",
    camera: "M4 8h3l2-3h6l2 3h3v11H4z M12 17a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7z",
    arrow: "M5 12h14 M13 6l6 6-6 6",
    bolt: "M13 2L4 14h7l-1 8 9-12h-7z",
    eye: "M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z",
    wand: "M4 20L15 9 M13 7l2-2 4 4-2 2z M6 3v3 M4.5 4.5h3 M19 14v3 M17.5 15.5h3",
    font: "M4 20L10 4h1l6 16 M6.5 14h8 M17 20h3",
  };

  function icon(name, size) {
    const s = size || 18;
    const svg = document.createElementNS(SVGNS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("width", s);
    svg.setAttribute("height", s);
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "1.8");
    svg.setAttribute("stroke-linecap", "round");
    svg.setAttribute("stroke-linejoin", "round");
    svg.setAttribute("aria-hidden", "true");
    const path = document.createElementNS(SVGNS, "path");
    path.setAttribute("d", ICONS[name] || ICONS.file);
    if (name === "play") { path.setAttribute("fill", "currentColor"); path.setAttribute("stroke", "none"); }
    svg.appendChild(path);
    return svg;
  }

  // ------------------------------------------------------------------ small utilities

  function fmtBytes(n) {
    if (n === null || n === undefined) return "";
    if (n < 1024) return n + " B";
    const u = ["KB", "MB", "GB", "TB"];
    let i = -1;
    do { n /= 1024; i++; } while (n >= 1024 && i < u.length - 1);
    return (n >= 100 ? n.toFixed(0) : n >= 10 ? n.toFixed(1) : n.toFixed(2)) + " " + u[i];
  }

  function fmtDur(sec) {
    if (sec === null || sec === undefined || isNaN(sec)) return "";
    if (sec < 60) return sec.toFixed(sec < 10 ? 1 : 0) + "s";
    const m = Math.floor(sec / 60), s = Math.round(sec % 60);
    if (m < 60) return m + "m " + String(s).padStart(2, "0") + "s";
    return Math.floor(m / 60) + "h " + String(m % 60).padStart(2, "0") + "m";
  }

  function fmtAgo(ts) {
    if (!ts) return "";
    const d = Date.now() / 1000 - ts;
    if (d < 45) return "just now";
    if (d < 3600) return Math.round(d / 60) + " min ago";
    if (d < 86400) return Math.round(d / 3600) + " h ago";
    if (d < 86400 * 7) return Math.round(d / 86400) + " d ago";
    return new Date(ts * 1000).toLocaleDateString();
  }

  function fmtClock(t) {
    t = Math.max(0, t || 0);
    return Math.floor(t / 60) + ":" + (t % 60).toFixed(1).padStart(4, "0");
  }

  function basename(p) { return String(p || "").replace(/\\/g, "/").split("/").pop(); }
  function dirname(p) { const a = String(p || "").split("/"); a.pop(); return a.join("/"); }
  function clone(o) { return JSON.parse(JSON.stringify(o)); }
  function num(v, d) { const n = parseFloat(v); return isFinite(n) ? n : d; }

  function joinPath(dir, rel) {
    const out = dir ? dir.split("/") : [];
    for (const part of rel.split("/")) {
      if (!part || part === ".") continue;
      if (part === "..") out.pop(); else out.push(part);
    }
    return out.join("/");
  }

  function fileUrl(pid, path, download) {
    const p = String(path).split("/").map(encodeURIComponent).join("/");
    return "/files/" + encodeURIComponent(pid) + "/" + p + (download ? "?download=1" : "");
  }

  const store = {
    get(key, dflt) {
      try { const v = localStorage.getItem("vstudio." + key); return v === null ? dflt : JSON.parse(v); }
      catch (e) { return dflt; }
    },
    set(key, value) {
      try { localStorage.setItem("vstudio." + key, JSON.stringify(value)); } catch (e) { /* private mode */ }
    },
  };

  const EXT = {
    video: /\.(mp4|mov|m4v|webm|mkv|avi)$/i,
    image: /\.(png|jpe?g|webp|gif|bmp)$/i,
    audio: /\.(mp3|wav|m4a|aac|ogg|flac|opus)$/i,
    json: /\.json$/i,
    captions: /\.(srt|vtt|json3?|ass)$/i,
    text: /\.(txt|md)$/i,
  };

  function typeOf(name) {
    for (const t of ["video", "image", "audio"]) if (EXT[t].test(name)) return t;
    if (/\.md$/i.test(name)) return "markdown";
    if (EXT.json.test(name)) return "json";
    return "file";
  }

  const TYPE_ICON = { video: "film", image: "image", audio: "music", markdown: "file", json: "file", link: "link", file: "file" };

  const KIND_LABEL = { reel: "Reel", autoedit: "Auto-edit", channel: "Channel", extract: "Style", render: "Render",
                       script: "Script", plan: "Plan", template: "Template" };
  const KIND_VIEW = { reel: "reel", autoedit: "autoedit", channel: "channel", extract: "style", script: "script",
                      render: "timeline", plan: "timeline", template: "templates" };
  const STATUS_LABEL = { queued: "Queued", running: "Running", done: "Done", failed: "Failed", cancelled: "Cancelled" };
  const ACTIVE = s => s === "queued" || s === "running";

  // ------------------------------------------------------------------ API

  async function api(method, url, body) {
    const opt = { method, headers: { Accept: "application/json" } };
    if (body !== undefined) {
      opt.headers["Content-Type"] = "application/json";
      opt.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(url, opt);
    } catch (e) {
      const err = new Error("The studio server is not answering. Is `python -m vstudio serve` still running?");
      err.status = 0;
      throw err;
    }
    const text = await res.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch (e) { data = null; }
    if (!res.ok) {
      const err = new Error((data && data.error) || (res.status + " " + res.statusText));
      err.status = res.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  async function fetchText(pid, path) {
    const res = await fetch(fileUrl(pid, path));
    if (!res.ok) throw new Error("could not open " + path + " (" + res.status + ")");
    return res.text();
  }

  function uploadFile(pid, file, onProgress) {
    const xhr = new XMLHttpRequest();
    const promise = new Promise((resolve, reject) => {
      xhr.open("PUT", "/api/projects/" + encodeURIComponent(pid) + "/upload?name=" + encodeURIComponent(file.name));
      xhr.setRequestHeader("Content-Type", "application/octet-stream");
      xhr.upload.onprogress = e => { if (e.lengthComputable) onProgress(e.loaded, e.total); };
      xhr.onload = () => {
        let data = null;
        try { data = JSON.parse(xhr.responseText); } catch (e) { data = null; }
        if (xhr.status >= 200 && xhr.status < 300) resolve(data);
        else reject(new Error((data && data.error) || "upload failed (" + xhr.status + ")"));
      };
      xhr.onerror = () => reject(new Error("upload failed: the connection dropped"));
      xhr.onabort = () => { const e = new Error("upload cancelled"); e.aborted = true; reject(e); };
      xhr.send(file);
    });
    return { promise, abort: () => xhr.abort() };
  }

  // ------------------------------------------------------------------ toasts

  function toast(title, opts) {
    opts = opts || {};
    const kind = opts.kind || "info";
    const box = $("#toasts");
    const close = () => {
      el.classList.add("out");
      setTimeout(() => el.remove(), 200);
    };
    const el = h("div", { class: "toast " + kind, role: kind === "bad" ? "alert" : "status" },
      h("span", { class: "t-icon" }, icon(kind === "ok" ? "check" : kind === "bad" ? "xcircle" : "info", 18)),
      h("div", { class: "t-body" },
        h("div", { class: "t-title", text: title }),
        opts.message ? h("div", { class: "t-msg", text: opts.message }) : null,
        opts.actions ? h("div", { class: "t-actions" }, opts.actions.map(a =>
          h("button", { class: "btn xs", type: "button", onclick: () => { close(); a.run(); } }, a.label))) : null),
      h("button", { class: "t-close", type: "button", "aria-label": "Dismiss", onclick: close }, icon("x", 15)));
    box.appendChild(el);
    while (box.children.length > 5) box.firstChild.remove();
    setTimeout(close, opts.timeout || (kind === "bad" ? 9000 : 5000));
  }

  function fail(err, title) {
    toast(title || "Something went wrong", { kind: "bad", message: err && err.message ? err.message : String(err) });
  }

  // ------------------------------------------------------------------ markdown (safe, DOM-building)

  function safeLink(href, resolve) {
    href = String(href || "").trim();
    if (/^(https?:\/\/|mailto:)/i.test(href)) return { url: href, external: true };
    if (/^[a-z][a-z0-9+.-]*:/i.test(href) || href.startsWith("//") || href.startsWith("#")) return null;
    const url = resolve ? resolve(href.split(/[?#]/)[0]) : null;
    return url ? { url, external: false } : null;
  }

  const INLINE_RE = /(`+)([\s\S]*?)\1|!\[([^\]]*)\]\(\s*<?([^)\s>]+)>?(?:\s+"[^"]*")?\s*\)|\[([^\]]+)\]\(\s*<?([^)\s>]+)>?(?:\s+"[^"]*")?\s*\)|\*\*([\s\S]+?)\*\*|__([\s\S]+?)__|\*([^*\s](?:[^*]*?[^*\s])?)\*|(?:^|(?<=[\s(]))_([^_\s](?:[^_]*?[^_\s])?)_(?=$|[\s).,;:!?])|<(https?:\/\/[^>\s]+)>|(https?:\/\/[^\s<>()]+[^\s<>().,;:!?'"])/g;

  function inline(text, resolve) {
    const frag = document.createDocumentFragment();
    const re = new RegExp(INLINE_RE.source, "g");
    let last = 0, m;
    while ((m = re.exec(text))) {
      if (m.index > last) frag.appendChild(document.createTextNode(text.slice(last, m.index)));
      if (m[1]) {
        frag.appendChild(h("code", { text: m[2].trim() }));
      } else if (m[4] !== undefined) {
        const link = safeLink(m[4], resolve);
        if (link && !link.external) frag.appendChild(h("img", { src: link.url, alt: m[3], loading: "lazy" }));
        else if (link) frag.appendChild(h("a", { href: link.url, target: "_blank", rel: "noopener noreferrer" }, "image: " + (m[3] || link.url)));
        else frag.appendChild(document.createTextNode(m[3]));
      } else if (m[6] !== undefined) {
        const link = safeLink(m[6], resolve);
        if (link) frag.appendChild(h("a", link.external ? { href: link.url, target: "_blank", rel: "noopener noreferrer" }
                                                        : { href: link.url, target: "_blank" }, inline(m[5], resolve)));
        else frag.appendChild(inline(m[5], resolve));
      } else if (m[7] !== undefined || m[8] !== undefined) {
        frag.appendChild(h("strong", null, inline(m[7] !== undefined ? m[7] : m[8], resolve)));
      } else if (m[9] !== undefined || m[10] !== undefined) {
        frag.appendChild(h("em", null, inline(m[9] !== undefined ? m[9] : m[10], resolve)));
      } else {
        const url = m[11] || m[12];
        frag.appendChild(h("a", { href: url, target: "_blank", rel: "noopener noreferrer" }, url));
      }
      last = re.lastIndex;
    }
    if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
    return frag;
  }

  function splitRow(line) {
    let s = line.trim();
    if (s.startsWith("|")) s = s.slice(1);
    if (s.endsWith("|") && !s.endsWith("\\|")) s = s.slice(0, -1);
    const cells = [];
    let cur = "";
    for (let i = 0; i < s.length; i++) {
      if (s[i] === "\\" && s[i + 1] === "|") { cur += "|"; i++; }
      else if (s[i] === "|") { cells.push(cur.trim()); cur = ""; }
      else cur += s[i];
    }
    cells.push(cur.trim());
    return cells;
  }

  const LIST_RE = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;
  const TABLE_SEP = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

  function isBlockStart(line, next) {
    return /^\s*(```|~~~)/.test(line) || /^#{1,6}\s/.test(line) || /^\s*>/.test(line) || LIST_RE.test(line) ||
      /^\s*([-*_])(\s*\1){2,}\s*$/.test(line) || (line.includes("|") && next !== undefined && TABLE_SEP.test(next));
  }

  function markdown(src, resolve) {
    const root = h("div", { class: "md" });
    const lines = String(src || "").replace(/\r\n?/g, "\n").split("\n");
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      let m;
      if (/^\s*$/.test(line)) { i++; continue; }
      if ((m = line.match(/^\s*(```|~~~)/))) {
        const fence = m[1];
        const body = [];
        i++;
        while (i < lines.length && !lines[i].trim().startsWith(fence)) body.push(lines[i++]);
        i++;
        root.appendChild(h("pre", null, h("code", { text: body.join("\n") })));
        continue;
      }
      if ((m = line.match(/^(#{1,6})\s+(.*?)\s*#*\s*$/))) {
        root.appendChild(h("h" + Math.min(m[1].length, 4), null, inline(m[2], resolve)));
        i++;
        continue;
      }
      if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { root.appendChild(h("hr")); i++; continue; }
      if (line.includes("|") && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1])) {
        const head = splitRow(line);
        const align = splitRow(lines[i + 1]).map(c => c.endsWith(":") ? (c.startsWith(":") ? "center" : "right") : "");
        i += 2;
        const tbody = h("tbody");
        while (i < lines.length && lines[i].includes("|") && lines[i].trim()) {
          const cells = splitRow(lines[i++]);
          tbody.appendChild(h("tr", null, head.map((_, c) =>
            h("td", align[c] ? { style: { textAlign: align[c] } } : null, inline(cells[c] || "", resolve)))));
        }
        root.appendChild(h("div", { class: "table-wrap" }, h("table", null,
          h("thead", null, h("tr", null, head.map((c, k) => h("th", align[k] ? { style: { textAlign: align[k] } } : null, inline(c, resolve))))),
          tbody)));
        continue;
      }
      if (/^\s*>/.test(line)) {
        const body = [];
        while (i < lines.length && /^\s*>/.test(lines[i])) body.push(lines[i++].replace(/^\s*>\s?/, ""));
        const inner = markdown(body.join("\n"), resolve);
        root.appendChild(h("blockquote", null, Array.from(inner.childNodes)));
        continue;
      }
      if (LIST_RE.test(line)) {
        const items = [];
        while (i < lines.length) {
          const l = lines[i];
          const lm = l.match(LIST_RE);
          if (lm) {
            items.push({ indent: lm[1].replace(/\t/g, "    ").length, ordered: /\d/.test(lm[2]), text: lm[3] });
            i++;
          } else if (/^\s*$/.test(l) && i + 1 < lines.length && LIST_RE.test(lines[i + 1])) {
            i++;
          } else if (/^\s{2,}\S/.test(l) && items.length) {
            items[items.length - 1].text += " " + l.trim();
            i++;
          } else break;
        }
        const stack = [];
        for (const it of items) {
          while (stack.length && it.indent < stack[stack.length - 1].indent) stack.pop();
          let level = stack[stack.length - 1];
          if (!level || it.indent > level.indent) {
            const list = h(it.ordered ? "ol" : "ul");
            if (level && level.list.lastChild) level.list.lastChild.appendChild(list);   // nested under the last item
            else root.appendChild(list);
            level = { indent: it.indent, list };
            stack.push(level);
          }
          const task = it.text.match(/^\[([ xX])\]\s+(.*)$/);
          level.list.appendChild(h("li", null, task ? [task[1] === " " ? "☐ " : "☑ ", inline(task[2], resolve)]
                                                     : inline(it.text, resolve)));
        }
        continue;
      }
      const para = [];
      while (i < lines.length && lines[i].trim() && (para.length === 0 || !isBlockStart(lines[i], lines[i + 1]))) {
        para.push(lines[i++].trim());
      }
      const p = h("p");
      para.forEach((l, k) => {
        if (k) p.appendChild(document.createTextNode(" "));
        p.appendChild(inline(l, resolve));
      });
      root.appendChild(p);
    }
    return root;
  }

  function mdResolver(pid, mdPath) {
    const dir = dirname(mdPath);
    return rel => fileUrl(pid, joinPath(dir, rel));
  }

  // ------------------------------------------------------------------ state

  const S = {
    projects: [],
    pid: null,
    project: null,
    jobs: [],
    health: null,
    doctor: null,
    templates: null,
    view: "projects",
    params: {},
    cache: {},            // view name -> {el, onShow}
    panels: new Set(),    // live job panels (update on poll)
    prevStatus: {},
    handoff: {},
    primary: null,        // Ctrl+Enter action of the current view
    pollTimer: null,
  };

  const VIEWS = {
    projects: { title: "Projects", sub: "Everything you make is kept in a project folder on this computer.", icon: "folder", render: viewProjects },
    reel: { title: "Make a Reel", sub: "Prompt + reference videos + your assets: a finished reel with motion graphics, captions and sound.", icon: "sparkles", render: viewReel, cache: true },
    autoedit: { title: "Auto-Edit", sub: "Raw talking video in, edited video out: cuts, reframing, zooms, graphics, captions, sound.", icon: "scissors", render: viewAutoEdit, cache: true },
    script: { title: "Script", sub: "A script that sounds like a person talking, written with the script-forge method and scored.", icon: "pen", render: viewScript, cache: true },
    channel: { title: "Channel Analysis", sub: "Thumbnails, titles, topics, dialogue, pacing and edit style of public channels and profiles.", icon: "chart", render: viewChannel, cache: true },
    style: { title: "Style Extract", sub: "The caption look, font, pacing, dialogue and edit DNA of one video.", icon: "scan", render: viewStyle, cache: true },
    timeline: { title: "Timeline", sub: "Fine-tune any render: layers, timing and text, or ask Claude to change it.", icon: "layers", render: viewTimeline },
    templates: { title: "Templates", sub: "The motion-graphics library. Preview any template at your aspect ratio.", icon: "grid", render: viewTemplates },
    jobs: { title: "Jobs", sub: "Everything running and finished, with live logs and outputs.", icon: "pulse", render: viewJobs },
    settings: { title: "Settings", sub: "Engine health, fonts, Claude Code and where your files live.", icon: "sliders", render: viewSettings },
  };

  const NAV = [
    { label: "Workspace", items: ["projects", "jobs"] },
    { label: "Create", items: ["reel", "autoedit", "script"] },
    { label: "Analyze", items: ["channel", "style"] },
    { label: "Edit", items: ["timeline", "templates"] },
    { label: "", items: ["settings"] },
  ];

  // ------------------------------------------------------------------ routing

  function parseHash() {
    const raw = location.hash.replace(/^#\/?/, "");
    const [name, query] = raw.split("?");
    const params = {};
    new URLSearchParams(query || "").forEach((v, k) => { params[k] = v; });
    return { view: VIEWS[name] ? name : "projects", params };
  }

  function go(view, params) {
    const q = params ? new URLSearchParams(params).toString() : "";
    const target = "#/" + view + (q ? "?" + q : "");
    if (location.hash === target) render();
    else location.hash = target;
  }

  function onHashChange() {
    if (S.leaveGuard && S.leaveGuard() && !confirm("Leave without saving your timeline changes?")) {
      history.replaceState(null, "", S.lastHash || "#/projects");
      return;
    }
    render();
  }

  function render() {
    S.leaveGuard = null;
    S.lastHash = location.hash;
    window.onbeforeunload = null;
    const { view, params } = parseHash();
    S.view = view;
    S.params = params;
    S.primary = null;
    S.panels.clear();
    const def = VIEWS[view];
    $("#page-title").textContent = def.title;
    $("#page-sub").textContent = def.sub;
    clear($("#topbar-actions"));
    document.title = def.title + " - Viral Studio";
    document.querySelectorAll(".nav-item").forEach(a => {
      if (a.dataset.view === view) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
    });
    const host = clear($("#view"));
    let entry = def.cache ? S.cache[view] : null;
    if (!entry) {
      entry = def.render() || {};
      if (entry instanceof Node) entry = { el: entry };
      if (def.cache) S.cache[view] = entry;
    }
    host.appendChild(entry.el);
    if (entry.onShow) entry.onShow(params);
    if (entry.panels) entry.panels.forEach(p => S.panels.add(p));
    if (entry.primary) S.primary = entry.primary;
    if (entry.actions) add($("#topbar-actions"), entry.actions);
    S.current = entry;
    updatePanels();
  }

  // ------------------------------------------------------------------ projects and polling

  async function refreshProjects() {
    S.projects = await api("GET", "/api/projects");
    const sel = $("#project-select");
    clear(sel);
    if (!S.projects.length) sel.appendChild(h("option", { value: "", text: "No projects yet" }));
    for (const p of S.projects) {
      if (p.name === "Template previews" && p.id !== S.pid) continue;
      sel.appendChild(h("option", { value: p.id, text: p.name }));
    }
    sel.value = S.pid || "";
  }

  async function loadProject() {
    if (!S.pid) { S.project = null; S.jobs = []; return; }
    try {
      S.project = await api("GET", "/api/projects/" + encodeURIComponent(S.pid));
      S.jobs = S.project.jobs || [];
      S.jobs.forEach(j => { S.prevStatus[j.id] = j.status; });
      const i = S.projects.findIndex(p => p.id === S.pid);
      if (i >= 0) Object.assign(S.projects[i], { uploads: S.project.upload_count, jobs: S.jobs.length,
                                                 updated: S.project.updated, last_job: S.jobs[0] || null });
    } catch (e) {
      if (e.status === 404) { S.pid = null; S.project = null; S.jobs = []; store.set("project", null); }
      else throw e;
    }
  }

  async function selectProject(pid, opts) {
    opts = opts || {};
    S.pid = pid || null;
    store.set("project", S.pid);
    if (!opts.keepViews) S.cache = {};
    await loadProject();
    await refreshProjects();
    updateBadge();
    if (!opts.silent) render();
  }

  async function ensureProject(hint) {
    if (S.pid) return S.pid;
    const name = (hint || "My project").replace(/\s+/g, " ").trim().slice(0, 60) || "My project";
    const p = await api("POST", "/api/projects", { name });
    await selectProject(p.id, { keepViews: true, silent: true });
    toast("New project: " + p.name, { kind: "info", timeout: 3500 });
    return p.id;
  }

  async function poll() {
    clearTimeout(S.pollTimer);
    let active = false;
    if (S.pid && document.visibilityState !== "hidden") {
      try {
        const list = await api("GET", "/api/projects/" + encodeURIComponent(S.pid) + "/jobs");
        for (const j of list) {
          const before = S.prevStatus[j.id];
          if (before && ACTIVE(before) && !ACTIVE(j.status)) announce(j);
          S.prevStatus[j.id] = j.status;
        }
        const changed = JSON.stringify(list) !== JSON.stringify(S.jobs);
        S.jobs = list;
        if (changed) { updateBadge(); updatePanels(); }
        active = list.some(j => ACTIVE(j.status));
      } catch (e) { /* the next poll retries; a dead server shows in the health dot */ }
    }
    S.pollTimer = setTimeout(poll, active ? 1200 : 4000);
  }

  function announce(job) {
    const view = KIND_VIEW[job.kind];
    const open = view && view !== "timeline" && view !== "templates" ? () => go(view) : () => go("jobs", { job: job.id });
    if (job.status === "done") {
      if (view !== S.view) toast("Finished: " + job.title, { kind: "ok", actions: [{ label: "Open", run: open }] });
      loadProject().then(updatePanels);
    } else if (job.status === "failed") {
      toast("Failed: " + job.title, { kind: "bad", message: job.error || "", actions: [{ label: "See log", run: () => go("jobs", { job: job.id }) }] });
    }
  }

  function updateBadge() {
    const n = S.jobs.filter(j => ACTIVE(j.status)).length;
    const badge = $("#jobs-badge");
    if (badge) { badge.textContent = n; badge.hidden = n === 0; }
  }

  function updatePanels() { S.panels.forEach(p => { try { p.update(); } catch (e) { console.error(e); } }); }

  function watchJob(id, onUpdate) {
    let stopped = false;
    const tick = async () => {
      if (stopped) return;
      try {
        const job = await api("GET", "/api/jobs/" + encodeURIComponent(id));
        onUpdate(job);
        if (!ACTIVE(job.status)) return;
      } catch (e) { onUpdate(null, e); return; }
      setTimeout(tick, 1000);
    };
    tick();
    return () => { stopped = true; };
  }

  async function submitJob(kind, args, hint) {
    const pid = await ensureProject(hint);
    const job = await api("POST", "/api/projects/" + encodeURIComponent(pid) + "/jobs", { kind, args });
    S.prevStatus[job.id] = job.status;
    S.jobs = [job].concat(S.jobs.filter(j => j.id !== job.id));
    updateBadge();
    updatePanels();
    poll();
    return job;
  }

  // ------------------------------------------------------------------ form components

  function Field(label, control, opts) {
    opts = opts || {};
    const id = control.id || ("f" + Math.random().toString(36).slice(2, 9));
    if (!control.id && /^(INPUT|TEXTAREA|SELECT)$/.test(control.tagName)) control.id = id;
    return h("div", { class: "field" + (opts.class ? " " + opts.class : "") },
      label ? h("label", { class: "field-label", for: control.id || null },
        h("span", null, label), opts.optional ? h("span", { class: "opt", text: "optional" }) : opts.right || null) : null,
      control,
      opts.hint ? h("p", { class: "field-hint", text: opts.hint }) : null);
  }

  function Seg(name, options, value, onChange) {
    const group = "seg-" + name + "-" + Math.random().toString(36).slice(2, 7);
    const el = h("div", { class: "seg", role: "radiogroup", "aria-label": name });
    options.forEach(o => {
      const input = h("input", { type: "radio", name: group, value: o.value, checked: o.value === value });
      input.addEventListener("change", () => { if (onChange) onChange(o.value); });
      el.appendChild(h("label", { title: o.title || null }, input,
        h("span", null, o.icon ? h("i", { class: "aspect-icon " + o.icon, "aria-hidden": "true" }) : null, o.label)));
    });
    return {
      el,
      value: () => { const c = el.querySelector("input:checked"); return c ? c.value : value; },
      set: v => { const c = el.querySelector("input[value=\"" + v + "\"]"); if (c) c.checked = true; },
    };
  }

  function Toggle(label, checked, hint, onChange) {
    const input = h("input", { type: "checkbox", checked: !!checked });
    if (onChange) input.addEventListener("change", () => onChange(input.checked));
    const el = h("label", { class: "toggle" }, input, h("span", { class: "track", "aria-hidden": "true" }),
      h("span", { class: "tg-text" }, h("span", { class: "tg-label", text: label }), hint ? h("span", { class: "tg-hint", text: hint }) : null));
    return { el, input, value: () => input.checked };
  }

  function Range(min, max, step, value, unit) {
    const input = h("input", { type: "range", min, max, step, value });
    const out = h("output", { class: "range-value" });
    const sync = () => {
      out.textContent = input.value + (unit || "");
      input.style.setProperty("--fill", ((input.value - min) / (max - min) * 100) + "%");
    };
    input.addEventListener("input", sync);
    sync();
    return { el: h("div", { class: "range-row" }, input, out), input, value: () => parseFloat(input.value) };
  }

  const ASPECT_OPTS = [
    { value: "9:16", label: "9:16", icon: "aspect-916", title: "Reels, Shorts, TikTok" },
    { value: "1:1", label: "1:1", icon: "aspect-11", title: "Square feed" },
    { value: "4:5", label: "4:5", icon: "aspect-45", title: "Instagram feed" },
    { value: "16:9", label: "16:9", icon: "aspect-169", title: "YouTube" },
  ];

  function acceptOf(types) {
    const map = { video: ".mp4,.mov,.m4v,.webm,.mkv,.avi,video/*", image: ".png,.jpg,.jpeg,.webp,.gif,image/*",
                  audio: ".mp3,.wav,.m4a,.aac,.ogg,.flac,.opus,audio/*", json: ".json", captions: ".srt,.vtt,.json,.json3,.ass",
                  text: ".txt,.md" };
    return types.includes("any") ? null : types.map(t => map[t]).join(",");
  }

  function accepts(types, name) {
    if (types.includes("any")) return true;
    return types.some(t => EXT[t] && EXT[t].test(name));
  }

  function looksLikeUrl(v) {
    v = v.trim();
    return /^https?:\/\/\S+$/i.test(v) || /^@[\w.-]{2,100}$/.test(v) ||
      /^(www\.)?[\w-]+(\.[\w-]+)*\.(com|be|tv|net|org|io|co|me|app|ly|gg|fm|us|uk|in)(\/\S*)?$/i.test(v);
  }

  /*
   * Dropzone: files dropped or picked are uploaded to the project at once
   * (with progress); links can be pasted when opts.urls; existing project
   * uploads can be picked. values() returns project paths and links.
   */
  function Dropzone(opts) {
    const types = opts.accept || ["any"];
    const items = [];
    const list = h("div", { class: "files" });
    const fileInput = h("input", { type: "file", class: "sr-only", tabindex: "-1", "aria-hidden": "true",
                                   multiple: !!opts.multiple, accept: acceptOf(types) });
    const zone = h("div", { class: "drop", tabindex: "0", role: "button",
                            "aria-label": opts.label + ": drop files here or press Enter to browse" },
      h("span", { class: "drop-icon" }, icon(opts.icon || "upload", 20)),
      h("span", { class: "drop-text" },
        h("span", { class: "drop-title" }, "Drop " + (opts.noun || "files") + " or ", h("u", null, "browse")),
        h("span", { class: "drop-hint", text: opts.hint || "" })));

    const changed = () => { if (opts.onChange) opts.onChange(values()); };

    function rowFor(item) {
      const meta = h("span", { class: "file-meta" });
      const barFill = h("i");
      const bar = h("div", { class: "bar" }, barFill);
      const remove = h("button", { class: "icon-btn sm plain", type: "button", "aria-label": "Remove " + item.name,
                                   onclick: () => { if (item.abort) item.abort(); removeItem(item); } }, icon("x", 15));
      const row = h("div", { class: "file-row" },
        h("span", { class: "file-icon " + item.type }, icon(TYPE_ICON[item.type] || "file", 16)),
        h("span", { class: "file-main" }, h("span", { class: "file-name", title: item.name, text: item.name }), meta, bar),
        h("span"), remove);
      item.paint = () => {
        meta.className = "file-meta" + (item.status === "error" ? " err" : "");
        if (item.status === "uploading") {
          const pct = item.total ? Math.round(item.loaded / item.total * 100) : 0;
          meta.textContent = "Uploading " + pct + "% · " + fmtBytes(item.loaded) + " of " + fmtBytes(item.total);
          barFill.style.width = pct + "%";
          bar.hidden = false;
        } else {
          bar.hidden = true;
          meta.textContent = item.status === "error" ? item.error :
            item.type === "link" ? "link" : [fmtBytes(item.size), item.path].filter(Boolean).join(" · ");
        }
      };
      item.paint();
      return row;
    }

    function addItem(item) {
      if (!opts.multiple) items.splice(0).forEach(it => { if (it.abort) it.abort(); it.row.remove(); });
      item.row = rowFor(item);
      items.push(item);
      list.appendChild(item.row);
      changed();
    }

    function removeItem(item) {
      const k = items.indexOf(item);
      if (k >= 0) items.splice(k, 1);
      item.row.remove();
      changed();
    }

    function addFiles(files) {
      const max = S.health ? S.health.max_upload : Infinity;
      let ok = Array.from(files).filter(f => {
        if (!accepts(types, f.name)) {
          toast("Not a " + types.join(" / ") + " file", { kind: "bad", message: f.name });
          return false;
        }
        if (f.size > max) {
          toast("File too large", { kind: "bad", message: f.name + " is " + fmtBytes(f.size) + "; the limit is " + fmtBytes(max) + "." });
          return false;
        }
        return true;
      });
      if (!opts.multiple) ok = ok.slice(0, 1);
      for (const f of ok) {
        const item = { name: f.name, type: typeOf(f.name), size: f.size, status: "uploading", loaded: 0, total: f.size };
        addItem(item);
        startUpload(item, f);
      }
    }

    async function startUpload(item, f) {
      let pid;
      try { pid = await ensureProject(opts.projectHint ? opts.projectHint() : null); }
      catch (e) { item.status = "error"; item.error = e.message; item.paint(); return; }
      const up = uploadFile(pid, f, (loaded, total) => { item.loaded = loaded; item.total = total; item.paint(); });
      item.abort = up.abort;
      try {
        const res = await up.promise;
        Object.assign(item, { status: "ready", path: res.path, name: res.name, size: res.size, abort: null });
        item.row.querySelector(".file-name").textContent = res.name;
        item.paint();
        changed();
        refreshUploadsSoon();
      } catch (e) {
        if (e.aborted) return;
        item.status = "error";
        item.error = e.message;
        item.paint();
        toast("Upload failed", { kind: "bad", message: f.name + ": " + e.message });
      }
    }

    function addPath(path) {
      if (items.some(i => i.path === path)) return;
      addItem({ name: basename(path), type: typeOf(path), path, status: "ready" });
    }

    function addUrl(url) {
      url = url.trim();
      if (!url) return;
      if (!looksLikeUrl(url)) { toast("That does not look like a link", { kind: "bad", message: url }); return; }
      if (items.some(i => i.url === url)) return;
      addItem({ name: url, type: "link", url, status: "ready" });
    }

    zone.addEventListener("click", () => fileInput.click());
    zone.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); } });
    fileInput.addEventListener("change", () => { addFiles(fileInput.files); fileInput.value = ""; });
    ["dragenter", "dragover"].forEach(ev => zone.addEventListener(ev, e => { e.preventDefault(); zone.classList.add("over"); }));
    ["dragleave", "dragend"].forEach(ev => zone.addEventListener(ev, () => zone.classList.remove("over")));
    zone.addEventListener("drop", e => {
      e.preventDefault();
      zone.classList.remove("over");
      if (e.dataTransfer.files && e.dataTransfer.files.length) addFiles(e.dataTransfer.files);
      else if (opts.urls) {
        const text = e.dataTransfer.getData("text/uri-list") || e.dataTransfer.getData("text/plain");
        text.split(/\s+/).filter(Boolean).forEach(addUrl);
      }
    });

    const tools = h("div", { class: "drop-tools" });
    if (opts.urls) {
      const urlInput = h("input", { class: "input sm", type: "url", placeholder: opts.urlPlaceholder || "Paste a link (YouTube, TikTok, Instagram...)",
                                    "aria-label": "Link for " + opts.label });
      const addBtn = h("button", { class: "btn sm", type: "button", onclick: () => { addUrl(urlInput.value); urlInput.value = ""; } }, icon("link", 14), "Add link");
      urlInput.addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); addBtn.click(); } });
      urlInput.addEventListener("paste", () => setTimeout(() => {
        const parts = urlInput.value.split(/\s+/).filter(Boolean);
        if (parts.length > 1) { parts.forEach(addUrl); urlInput.value = ""; }
      }, 0));
      add(tools, [urlInput, addBtn]);
    }
    const pick = h("select", { class: "select sm", "aria-label": "Add a file already in the project" });
    pick.addEventListener("change", () => { if (pick.value) addPath(pick.value); pick.value = ""; });
    const refreshPick = () => {
      clear(pick);
      const ups = ((S.project && S.project.uploads) || []).filter(u => accepts(types, u.name));
      const extra = opts.extraChoices ? opts.extraChoices() : [];
      pick.appendChild(h("option", { value: "", text: ups.length || extra.length ? "Add from project..." : "No matching project files" }));
      if (ups.length) pick.appendChild(h("optgroup", { label: "Uploads" }, ups.map(u => h("option", { value: u.path, text: u.name }))));
      if (extra.length) pick.appendChild(h("optgroup", { label: opts.extraLabel || "Results" }, extra.map(x => h("option", { value: x.path, text: x.label }))));
      pick.hidden = !ups.length && !extra.length;
    };
    refreshPick();
    if (opts.fromProject !== false) tools.appendChild(pick);

    function values() { return items.filter(i => i.status === "ready").map(i => i.path || i.url); }

    const el = h("div", { class: "field" },
      h("div", { class: "field-label" }, h("span", null, opts.label), opts.optional ? h("span", { class: "opt", text: "optional" }) : null),
      zone, fileInput, tools.childNodes.length ? tools : null, list);
    return {
      el, values,
      value: () => values()[0] || null,
      busy: () => items.some(i => i.status === "uploading"),
      set: vals => { (Array.isArray(vals) ? vals : [vals]).filter(Boolean).forEach(v => looksLikeUrl(v) ? addUrl(v) : addPath(v)); },
      clear: () => { items.splice(0).forEach(i => { if (i.abort) i.abort(); i.row.remove(); }); changed(); },
      refresh: refreshPick,
    };
  }

  let uploadsTimer = null;
  function refreshUploadsSoon() {
    clearTimeout(uploadsTimer);
    uploadsTimer = setTimeout(async () => {
      await loadProject();
      Object.values(S.cache).forEach(c => c.refresh && c.refresh());
      if (S.current && !VIEWS[S.view].cache && S.current.refresh) S.current.refresh();
    }, 400);
  }

  function SubmitButton(label, run) {
    const btn = h("button", { class: "btn primary big block", type: "button" }, icon("bolt", 18), h("span", { text: label }),
      h("span", { class: "kbd", "aria-hidden": "true", text: navigator.platform.indexOf("Mac") >= 0 ? "⌘↵" : "Ctrl ↵" }));
    let busy = false;
    const go2 = async () => {
      if (busy) return;
      busy = true;
      btn.disabled = true;
      try { await run(); } catch (e) { fail(e, "Could not start"); } finally { busy = false; btn.disabled = false; }
    };
    btn.addEventListener("click", go2);
    return { el: btn, run: go2 };
  }

  function needUploadsDone(zones) {
    if (zones.some(z => z.busy())) {
      toast("Uploads are still running", { kind: "info", message: "Start again when every file shows its size." });
      return false;
    }
    return true;
  }

  // ------------------------------------------------------------------ job display

  function statusPill(job) {
    let label = STATUS_LABEL[job.status] || job.status;
    if (job.status === "queued" && job.queue_position) label += " #" + job.queue_position;
    return h("span", { class: "pill " + job.status, text: label });
  }

  function progressLine(job) {
    const known = job.progress !== null && job.progress !== undefined;
    const fill = h("i");
    const bar = h("div", { class: "bar lg" + (known || job.status !== "running" ? "" : " indeterminate") }, fill);
    fill.style.width = (job.status === "done" ? 100 : known ? Math.round(job.progress * 100) : 0) + "%";
    return h("div", { class: "progress-line" }, bar,
      h("span", { class: "progress-pct", text: known ? Math.round(job.progress * 100) + "%" : job.status === "queued" ? "waiting" : "" }));
  }

  function logClass(line) {
    if (line.startsWith("$ ")) return "l-cmd";
    if (line.startsWith("== ") || line.startsWith("note: ")) return "l-sys";
    if (/\b(error|traceback|failed|exception)\b/i.test(line)) return "l-err";
    if (/^(wrote|done|saved)\b/i.test(line)) return "l-ok";
    return null;
  }

  function cookLog(text) {
    return text.replace(/\x1b\[[0-9;?]*[A-Za-z]/g, "").split("\n").map(l => {
      const parts = l.split("\r").filter((p, k, arr) => p !== "" || k === arr.length - 1);
      return parts.length ? parts[parts.length - 1] : "";
    });
  }

  /* Live log: tails /api/jobs/<id>/log while isActive() says so. */
  function LogView(jobId, isActive, short) {
    const pre = h("pre", { class: "log" + (short ? " short" : ""), tabindex: "0", "aria-label": "Job log" });
    let buf = "", offset = 0, stopped = false, timer = null, rendered = null;
    const paint = () => {
      if (buf === rendered) return;
      const atBottom = rendered === null || pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
      rendered = buf;
      clear(pre);
      const lines = cookLog(buf);
      for (const line of lines) {
        const cls = logClass(line);
        pre.appendChild(cls ? h("span", { class: cls, text: line }) : document.createTextNode(line));
        pre.appendChild(document.createTextNode("\n"));
      }
      if (atBottom) pre.scrollTop = pre.scrollHeight;
    };
    const tick = async () => {
      if (stopped || !pre.isConnected && offset > 0) return;
      try {
        const r = await api("GET", "/api/jobs/" + encodeURIComponent(jobId) + "/log?offset=" + offset);
        if (r.offset < offset) buf = "";
        buf = (buf + r.text).slice(-400000);
        offset = r.offset;
        paint();
      } catch (e) { /* retry */ }
      if (!stopped && isActive()) timer = setTimeout(tick, 1000);
      else if (!stopped) { setTimeout(async () => {      // one last read after the job ends
        try { const r = await api("GET", "/api/jobs/" + encodeURIComponent(jobId) + "/log?offset=" + offset);
              buf = (buf + r.text).slice(-400000); offset = r.offset; paint(); } catch (e) { /* ignore */ }
      }, 400); }
    };
    tick();
    return { el: pre, stop: () => { stopped = true; clearTimeout(timer); }, poke: () => { if (!timer) tick(); } };
  }

  function playerFor(pid, path, aspect) {
    const cls = aspect === "16:9" ? "landscape" : aspect === "1:1" ? "square" : "portrait";
    const video = h("video", { src: fileUrl(pid, path) + "#t=0.001", controls: true, playsinline: true, preload: "metadata" });
    const box = h("div", { class: "player " + cls }, video);
    video.addEventListener("loadedmetadata", () => {
      const r = video.videoWidth / Math.max(1, video.videoHeight);
      box.className = "player " + (r > 1.2 ? "landscape" : r > 0.9 ? "square" : "portrait");
    });
    return { el: box, video };
  }

  function aspectOfJob(job) { return (job.args && job.args.aspect) || "9:16"; }

  function outputsGrid(job, onOpen) {
    const outs = (job.outputs || []).slice(0, 120);
    if (!outs.length) return h("p", { class: "muted small", text: "No output files yet." });
    return h("div", { class: "outputs" }, outs.map(o => {
      const thumb = h("div", { class: "out-thumb" });
      if (o.type === "image") thumb.appendChild(h("img", { src: fileUrl(job.project, o.path), alt: "", loading: "lazy" }));
      else thumb.appendChild(icon(TYPE_ICON[o.type] || "file", 28));
      return h("div", { class: "out-tile" },
        h("button", { class: "out-thumb-btn", type: "button", title: "Open " + o.name,
                      onclick: () => onOpen(o) }, thumb),
        h("div", { class: "out-name", title: o.path, text: o.name }),
        h("div", { class: "row-between" }, h("span", { class: "muted small", text: fmtBytes(o.size) }),
          h("div", { class: "out-actions" },
            h("button", { class: "icon-btn sm plain", type: "button", title: "Open", "aria-label": "Open " + o.name, onclick: () => onOpen(o) }, icon("eye", 15)),
            h("a", { class: "icon-btn sm plain", href: fileUrl(job.project, o.path, true), title: "Download", "aria-label": "Download " + o.name }, icon("download", 15)))));
    }));
  }

  async function viewerFor(job, o) {
    const pid = job.project;
    if (o.type === "video") return playerFor(pid, o.path, aspectOfJob(job)).el;
    if (o.type === "image") return h("a", { href: fileUrl(pid, o.path), target: "_blank" }, h("img", { src: fileUrl(pid, o.path), alt: o.name, class: "player", style: { maxHeight: "70vh", objectFit: "contain" } }));
    if (o.type === "audio") return h("audio", { src: fileUrl(pid, o.path), controls: true, style: { width: "100%" } });
    const text = await fetchText(pid, o.path);
    if (o.type === "markdown") return markdown(text, mdResolver(pid, o.path));
    let shown = text;
    if (o.type === "json") { try { shown = JSON.stringify(JSON.parse(text), null, 2); } catch (e) { shown = text; } }
    return h("pre", { class: "log", style: { maxHeight: "560px" }, text: shown.length > 300000 ? shown.slice(0, 300000) + "\n..." : shown });
  }

  function findOutput(job, name) {
    const prefix = "jobs/" + job.id + "/out/";
    return (job.outputs || []).find(o => o.path === prefix + name) || null;
  }

  const detailCache = {};
  async function jobDetail(id, force) {
    if (!force && detailCache[id] && !ACTIVE(detailCache[id].status)) return detailCache[id];
    const d = await api("GET", "/api/jobs/" + encodeURIComponent(id));
    detailCache[id] = d;
    return d;
  }

  /*
   * JobPanel: the latest job of some kinds in the current project, live.
   * Running: progress, stage, cancel, log. Done: opts.renderDone(job) (outputs).
   */
  function JobPanel(opts) {
    const root = h("div", { class: "job-panel" });
    let sig = "", pinned = null, log = null;
    const picker = h("select", { class: "select sm", "aria-label": "Show an earlier run" });
    picker.addEventListener("change", () => { pinned = picker.value || null; sig = ""; update(); });

    function jobsOfKind() { return S.jobs.filter(j => opts.kinds.includes(j.kind)); }

    function update() {
      const list = jobsOfKind();
      const job = (pinned && list.find(j => j.id === pinned)) || list[0] || null;
      const next = job ? [job.id, job.status, job.progress, job.stage, list.length].join("|") : "none|" + !!S.pid;
      if (next === sig) return;
      const sameJobRunning = job && log && sig.split("|")[0] === job.id && ACTIVE(job.status);
      sig = next;
      if (sameJobRunning) { paintRunning(job, true); return; }
      if (log) { log.stop(); log = null; }
      clear(root);
      clear(picker);
      list.forEach(j => picker.appendChild(h("option", { value: j.id, text: j.title + " · " + fmtAgo(j.created) })));
      picker.value = job ? job.id : "";
      if (!job) {
        root.appendChild(h("div", { class: "empty" }, h("div", { class: "empty-art" }, icon(opts.emptyIcon || "sparkles", 26)),
          h("h3", { text: opts.emptyTitle }), h("p", { text: opts.emptyText })));
        return;
      }
      root.appendChild(h("div", { class: "job-head" },
        h("div", { class: "grow", style: { minWidth: 0 } }, h("div", { class: "job-title", title: job.title, text: job.title }),
          h("div", { class: "muted small", text: (KIND_LABEL[job.kind] || job.kind) + " · started " + fmtAgo(job.created) + (job.elapsed ? " · " + fmtDur(job.elapsed) : "") })),
        statusPill(job)));
      if (list.length > 1) root.appendChild(picker);
      if (ACTIVE(job.status)) paintRunning(job, false);
      else if (job.status === "done") paintDone(job);
      else paintFailed(job);
    }

    function paintRunning(job, refresh) {
      if (refresh) {
        const old = root.querySelector(".running-box");
        if (old) old.replaceWith(runningBox(job));
        return;
      }
      root.appendChild(runningBox(job));
      log = LogView(job.id, () => { const j = S.jobs.find(x => x.id === job.id); return j && ACTIVE(j.status); }, true);
      root.appendChild(h("details", { class: "small" }, h("summary", { class: "muted", text: "Live log" }), log.el));
    }

    function runningBox(job) {
      return h("div", { class: "running-box stack", style: { gap: "10px" } },
        progressLine(job),
        h("div", { class: "row-between" },
          h("div", { class: "job-stage grow", title: job.stage || "", text: job.status === "queued" ? "Waiting for the previous render to finish..." : (job.stage || "Starting...") }),
          h("button", { class: "btn sm danger", type: "button", onclick: () => cancelJob(job.id) }, icon("stop", 13), "Cancel")));
    }

    async function paintDone(job) {
      const box = h("div", { class: "stack" }, h("div", { class: "row muted small" }, h("span", { class: "spinner dark" }), "Loading results..."));
      root.appendChild(box);
      try {
        const d = await jobDetail(job.id, true);
        clear(box);
        add(box, await opts.renderDone(d));
      } catch (e) {
        clear(box);
        box.appendChild(h("div", { class: "callout bad" }, icon("xcircle"), h("div", { text: e.message })));
      }
    }

    function paintFailed(job) {
      root.appendChild(h("div", { class: "callout " + (job.status === "cancelled" ? "warn" : "bad") },
        icon(job.status === "cancelled" ? "warn" : "xcircle"),
        h("div", null, h("b", { text: job.status === "cancelled" ? "Cancelled." : "This run failed." }), " ",
          job.status === "failed" ? (job.error || "See the log for details.") : "")));
      root.appendChild(h("div", { class: "row" },
        h("button", { class: "btn sm", type: "button", onclick: () => go("jobs", { job: job.id }) }, icon("pulse", 14), "Open log"),
        h("button", { class: "btn sm", type: "button", onclick: () => rerun(job) }, icon("refresh", 14), "Run again")));
    }

    return { el: root, update, reset: () => { sig = ""; pinned = null; } };
  }

  async function cancelJob(id) {
    try {
      await api("POST", "/api/jobs/" + encodeURIComponent(id) + "/cancel");
      toast("Cancelling", { kind: "info", message: "The job and everything it started are being stopped." });
      poll();
    } catch (e) { fail(e, "Could not cancel"); }
  }

  async function rerun(job) {
    try {
      await api("POST", "/api/projects/" + encodeURIComponent(job.project) + "/jobs", { kind: job.kind, args: job.args || {} });
      toast("Started again: " + job.title, { kind: "info" });
      poll();
    } catch (e) { fail(e, "Could not start"); }
  }

  async function mdSection(job, name, title, open) {
    const o = findOutput(job, name);
    if (!o) return null;
    try {
      const text = await fetchText(job.project, o.path);
      return h("details", { class: "card", style: { padding: "14px 16px" }, open: !!open },
        h("summary", { class: "card-title", style: { cursor: "pointer" } }, icon("file", 15), title),
        h("div", { style: { marginTop: "12px" } }, markdown(text, mdResolver(job.project, o.path))));
    } catch (e) { return null; }
  }

  function videoResult(job, names) {
    for (const n of names) {
      const o = findOutput(job, n);
      if (o) return { o, player: playerFor(job.project, o.path, aspectOfJob(job)) };
    }
    const any = (job.outputs || []).find(o => o.type === "video");
    return any ? { o: any, player: playerFor(job.project, any.path, aspectOfJob(job)) } : null;
  }

  function specOf(job, names) {
    for (const n of names) { const o = findOutput(job, n); if (o) return o.path; }
    return null;
  }

  // ------------------------------------------------------------------ views: projects

  function quickTile(view, iconName, title, sub) {
    return h("a", { href: "#/" + view }, h("span", { class: "q-icon" }, icon(iconName, 18)),
      h("span", { class: "q-title", text: title }), h("span", { class: "q-sub", text: sub }));
  }

  function newProjectDialog() {
    const input = h("input", { class: "input", placeholder: "e.g. iPhone tips series", maxlength: "80", "aria-label": "Project name" });
    const close = () => back.remove();
    const create = async () => {
      try {
        const p = await api("POST", "/api/projects", { name: input.value || "Untitled project" });
        close();
        await selectProject(p.id);
        toast("Project created", { kind: "ok", message: p.name });
      } catch (e) { fail(e, "Could not create the project"); }
    };
    input.addEventListener("keydown", e => { if (e.key === "Enter") create(); });
    const back = h("div", { class: "modal-back", onclick: e => { if (e.target === back) close(); } },
      h("div", { class: "modal", role: "dialog", "aria-modal": "true", "aria-label": "New project" },
        h("h2", { text: "New project" }),
        h("p", { text: "A folder for one piece of work: uploads, renders, analyses and timelines stay together." }),
        Field("Name", input),
        h("div", { class: "row" }, h("button", { class: "btn ghost", type: "button", onclick: close }, "Cancel"),
          h("button", { class: "btn primary", type: "button", onclick: create }, icon("plus", 16), "Create"))));
    back.addEventListener("keydown", e => { if (e.key === "Escape") close(); });
    document.body.appendChild(back);
    input.focus();
  }

  function viewProjects() {
    const el = h("div", { class: "fade-in" });
    el.appendChild(h("section", { class: "hero" },
      h("h2", { text: "What are we making today?" }),
      h("p", { text: "Everything renders on this computer. Claude plans and writes through your own Claude Code login; nothing is uploaded to a render service." }),
      h("div", { class: "quick" },
        quickTile("reel", "sparkles", "Make a reel", "prompt + references"),
        quickTile("autoedit", "scissors", "Auto-edit", "raw video in, edit out"),
        quickTile("script", "pen", "Write a script", "scored before you shoot"),
        quickTile("channel", "chart", "Analyze a channel", "what makes it work"),
        quickTile("style", "scan", "Extract a style", "font, pace, edit"))));

    const visible = S.projects.filter(p => p.name !== "Template previews" || p.id === S.pid);
    if (!visible.length) {
      el.appendChild(h("div", { class: "card" }, h("div", { class: "empty" }, h("div", { class: "empty-art" }, icon("folder", 26)),
        h("h3", { text: "No projects yet" }),
        h("p", { text: "Start anything above and a project is created for you, or make one now." }),
        h("button", { class: "btn primary", type: "button", onclick: newProjectDialog }, icon("plus", 16), "New project"))));
      return { el, actions: [h("button", { class: "btn sm", type: "button", onclick: newProjectDialog }, icon("plus", 15), "New project")] };
    }
    const grid = h("div", { class: "project-grid" });
    for (const p of visible) {
      const cover = h("div", { class: "pcard-cover" });
      if (p.cover) cover.appendChild(h("img", { src: fileUrl(p.id, p.cover), alt: "", loading: "lazy" }));
      else cover.appendChild(h("span", { class: "pcard-letter", text: (p.name || "?").slice(0, 1).toUpperCase() }));
      const last = p.last_job;
      grid.appendChild(h("button", { class: "pcard" + (p.id === S.pid ? " active" : ""), type: "button",
                                     onclick: () => selectProject(p.id), "aria-pressed": p.id === S.pid ? "true" : "false" },
        cover,
        h("span", { class: "pcard-body" },
          h("span", { class: "pcard-name", title: p.name, text: p.name }),
          h("span", { class: "pcard-meta", text: p.uploads + " upload" + (p.uploads === 1 ? "" : "s") + " · " + p.jobs + " job" + (p.jobs === 1 ? "" : "s") + " · " + fmtAgo(p.updated) }),
          last ? h("span", { class: "row", style: { gap: "6px" } }, statusPill(last), h("span", { class: "muted small ellipsis", text: last.title })) : null)));
    }
    grid.appendChild(h("button", { class: "pcard new", type: "button", onclick: newProjectDialog }, icon("plus", 22), "New project"));
    el.appendChild(h("div", { class: "card-head", style: { marginTop: "6px" } }, h("h2", { class: "card-title", text: "Projects" })));
    el.appendChild(grid);

    let refresh = null;
    if (S.project) {
      const P = S.project;
      const uploader = Dropzone({ label: "Add files to " + P.name, multiple: true, accept: ["any"], fromProject: false,
                                  hint: "Videos, photos, logos, music, captions. Kept in this project's uploads folder." });
      const uploadsBox = h("div", { style: { marginTop: "14px" } });
      const uploadsTable = () => S.project.uploads.length ? h("table", { class: "table" },
        h("thead", null, h("tr", null, h("th", { text: "File" }), h("th", { text: "Type" }), h("th", { class: "num", text: "Size" }), h("th", { text: "" }))),
        h("tbody", null, S.project.uploads.map(u => h("tr", null,
          h("td", null, h("a", { href: fileUrl(P.id, u.path), target: "_blank", text: u.name })),
          h("td", { class: "muted", text: u.type }),
          h("td", { class: "num muted", text: fmtBytes(u.size) }),
          h("td", { class: "num" }, h("button", { class: "icon-btn sm plain", type: "button", "aria-label": "Delete " + u.name, title: "Delete",
            onclick: async () => {
              if (!confirm("Delete " + u.name + " from this project?")) return;
              try { await api("DELETE", "/api/projects/" + encodeURIComponent(P.id) + "/upload?name=" + encodeURIComponent(u.name)); await loadProject(); clear(uploadsBox).appendChild(uploadsTable()); }
              catch (e) { fail(e, "Could not delete"); }
            } }, icon("trash", 15))))))) : h("p", { class: "muted small", text: "No uploads yet." });
      uploadsBox.appendChild(uploadsTable());
      refresh = () => clear(uploadsBox).appendChild(uploadsTable());   // uploads in progress keep their rows

      const specs = P.specs.filter(s => s.kind === "spec");
      const specList = specs.length ? h("table", { class: "table" }, h("tbody", null, specs.slice(0, 12).map(s => h("tr", { class: "click", tabindex: "0",
          onclick: () => go("timeline", { path: s.path }), onkeydown: e => { if (e.key === "Enter") go("timeline", { path: s.path }); } },
        h("td", { class: "cell-fill" }, h("div", { class: "row", style: { gap: "8px", flexWrap: "nowrap" } }, icon("layers", 15),
          h("div", { style: { minWidth: 0 } }, h("div", { class: "ellipsis", style: { fontWeight: 600 }, text: s.name }),
            h("div", { class: "muted small ellipsis", title: s.path, text: s.path })))),
        h("td", { class: "num muted small nowrap", text: fmtAgo(s.mtime) })))))
        : h("p", { class: "muted small", text: "Timelines appear here after a reel, an auto-edit or a plan compile." });

      const jobsTable = P.jobs.length ? h("table", { class: "table" }, h("tbody", null, P.jobs.slice(0, 10).map(j => h("tr", { class: "click", tabindex: "0",
          onclick: () => go("jobs", { job: j.id }), onkeydown: e => { if (e.key === "Enter") go("jobs", { job: j.id }); } },
        h("td", null, statusPill(j)),
        h("td", null, h("span", { class: "pill kind plain", text: KIND_LABEL[j.kind] || j.kind })),
        h("td", { class: "cell-fill", title: j.title, text: j.title }),
        h("td", { class: "num muted small nowrap", text: fmtAgo(j.created) })))))
        : h("p", { class: "muted small", text: "Nothing has run in this project yet." });

      el.appendChild(h("div", { class: "split", style: { marginTop: "24px" } },
        h("div", { class: "stack" },
          h("section", { class: "card" }, h("div", { class: "card-head" }, h("h2", { class: "card-title" }, icon("upload", 16), "Uploads"),
            h("span", { class: "muted small", text: P.uploads.length + " file" + (P.uploads.length === 1 ? "" : "s") })), uploader.el, uploadsBox)),
        h("div", { class: "stack" },
          h("section", { class: "card" }, h("div", { class: "card-head" }, h("h2", { class: "card-title" }, icon("layers", 16), "Timelines")), specList),
          h("section", { class: "card" }, h("div", { class: "card-head" }, h("h2", { class: "card-title" }, icon("pulse", 16), "Recent jobs"),
            h("a", { href: "#/jobs", class: "small", text: "All jobs" })), jobsTable))));
    }
    return { el, refresh, actions: [h("button", { class: "btn sm", type: "button", onclick: newProjectDialog }, icon("plus", 15), "New project")] };
  }

  // ------------------------------------------------------------------ views: make a reel

  function claudeHint() {
    if (!S.health) return "Uses your Claude subscription through Claude Code.";
    return S.health.claude ? "Uses your Claude subscription through Claude Code. Off: the offline planner."
                           : "Claude Code was not found on this computer (see Settings); the offline planner is used.";
  }

  function stepsCard(steps) {
    return h("section", { class: "card" }, h("h2", { class: "card-title", style: { marginBottom: "12px" } }, icon("info", 16), "How it works"),
      h("ol", { class: "md", style: { margin: 0, paddingLeft: "20px", fontSize: "13px" } }, steps.map(s => h("li", { text: s }))));
  }

  function viewReel() {
    const prompt = h("textarea", { class: "textarea big", maxlength: "4000", rows: "4",
      placeholder: "3 iPhone camera settings that make videos look expensive. Punchy and fast, end with “follow for part 2”." });
    const counter = h("span", { class: "counter", text: "0 / 4000" });
    prompt.addEventListener("input", () => { counter.textContent = prompt.value.length + " / 4000"; });
    const hint = () => prompt.value.trim().split(/\s+/).slice(0, 6).join(" ") || "New reel";
    const refs = Dropzone({ label: "Reference videos", multiple: true, accept: ["video"], urls: true, noun: "videos", icon: "film",
      hint: "The style to copy: captions, cuts, pace, energy. Files or links.", projectHint: hint });
    const comps = Dropzone({ label: "Competitor videos", multiple: true, accept: ["video"], urls: true, noun: "videos", icon: "chart", optional: true,
      hint: "The bar to beat: hooks and structure are measured, never copied.", projectHint: hint });
    const assets = Dropzone({ label: "Your clips, photos and logo", multiple: true, accept: ["video", "image"], noun: "media", icon: "image", optional: true,
      hint: "Used as backgrounds and B-roll. Your own footage only.", projectHint: hint });
    const script = h("textarea", { class: "textarea", rows: "4", placeholder: "Paste the exact words to say, or leave empty and the planner writes them." });
    const voice = Dropzone({ label: "Voiceover", accept: ["audio", "video"], noun: "a voiceover", icon: "wave", optional: true, hint: "Its words drive the captions.", projectHint: hint });
    const music = Dropzone({ label: "Music", accept: ["audio"], noun: "a track", icon: "music", optional: true, hint: "Ducked under the voice automatically.", projectHint: hint });
    const aspect = Seg("Aspect ratio", ASPECT_OPTS, store.get("reel.aspect", "9:16"), v => store.set("reel.aspect", v));
    const duration = Range(5, 90, 1, 30, " s");
    const llm = Toggle("Plan with Claude", true, claudeHint());
    const quality = Seg("Quality", [{ value: "preview", label: "Preview (fast)" }, { value: "final", label: "Final 1080p" }], "preview");

    const panel = JobPanel({ kinds: ["reel"], emptyIcon: "sparkles", emptyTitle: "Your reel shows up here",
      emptyText: "Describe it, add a reference video or link, and press Make the reel.",
      renderDone: async job => {
        const out = [];
        const v = videoResult(job, ["reel.mp4"]);
        const spec = specOf(job, ["spec.json"]);
        if (v) out.push(v.player.el);
        else out.push(h("div", { class: "callout info" }, icon("info"), h("div", { text: "No video was rendered for this run (planning only). Open the timeline to render it." })));
        out.push(h("div", { class: "player-actions" },
          v ? h("a", { class: "btn sm", href: fileUrl(job.project, v.o.path, true) }, icon("download", 14), "Download") : null,
          spec ? h("button", { class: "btn sm", type: "button", onclick: () => go("timeline", { path: spec }) }, icon("layers", 14), "Open timeline") : null,
          spec && job.args && job.args.preview ? h("button", { class: "btn sm primary", type: "button", onclick: async () => {
            try { await submitJob("render", { spec }); toast("Rendering full quality", { kind: "info", message: "Follow it in Jobs or on the Timeline page." }); }
            catch (e) { fail(e, "Could not start the render"); }
          } }, icon("bolt", 14), "Render final") : null));
        // one still per beat, taken by the QA pass: the quickest visual review of the whole reel
        const sheet = findOutput(job, "qa/contact_sheet.jpg");
        if (sheet) out.push(h("a", { href: fileUrl(job.project, sheet.path), target: "_blank", style: { display: "block", marginTop: "14px" } },
          h("img", { src: fileUrl(job.project, sheet.path), alt: "Stills of every beat", style: { maxWidth: "100%", borderRadius: "12px", border: "1px solid var(--line)" } })));
        const qa = await mdSection(job, "QA.md", "QA: output vs the reference", false);
        const brief = await mdSection(job, "BRIEF.md", "Brief", false);
        return out.concat([qa, brief].filter(Boolean));
      } });

    const submit = SubmitButton("Make the reel", async () => {
      if (!prompt.value.trim()) { prompt.focus(); toast("Describe the reel first", { kind: "info", message: "One or two sentences: topic, angle, energy, ending." }); return; }
      if (!needUploadsDone([refs, comps, assets, voice, music])) return;
      const args = { prompt: prompt.value.trim(), refs: refs.values(), competitors: comps.values(), assets: assets.values(),
                     aspect: aspect.value(), duration: duration.value(), llm: llm.value() ? "auto" : "none",
                     preview: quality.value() === "preview" };
      if (script.value.trim()) args.script_text = script.value.trim();
      if (voice.value()) args.voice = voice.value();
      if (music.value()) args.music = music.value();
      const job = await submitJob("reel", args, hint());
      panel.reset();
      updatePanels();
      if (job.status === "queued") toast("Queued", { kind: "info", message: "It starts when the current render finishes." });
    });

    const el = h("div", { class: "split fade-in" },
      h("div", { class: "stack" },
        h("section", { class: "card" }, h("div", { class: "card-head" }, h("h2", { class: "card-title" }, h("span", { class: "num", text: "1" }), "The idea")),
          Field("Prompt", prompt, { right: counter, hint: "Topic, angle, who it's for, energy, how it ends. Facts and numbers only if they're real." })),
        h("section", { class: "card" }, h("div", { class: "card-head" }, h("h2", { class: "card-title" }, h("span", { class: "num", text: "2" }), "References")),
          h("div", { class: "stack" }, refs.el, comps.el)),
        h("section", { class: "card" }, h("div", { class: "card-head" }, h("h2", { class: "card-title" }, h("span", { class: "num", text: "3" }), "Your material")),
          h("div", { class: "stack" }, assets.el, Field("Script", script, { optional: true }), h("div", { class: "grid-2" }, voice.el, music.el))),
        h("section", { class: "card" }, h("div", { class: "card-head" }, h("h2", { class: "card-title" }, h("span", { class: "num", text: "4" }), "Format")),
          h("div", { class: "stack" },
            h("div", { class: "grid-2" }, Field("Aspect ratio", aspect.el), Field("Quality", quality.el)),
            Field("Length", duration.el),
            llm.el)),
        submit.el),
      h("div", { class: "stack sticky" },
        h("section", { class: "card" }, h("div", { class: "card-head" }, h("h2", { class: "card-title" }, icon("play", 15), "Latest reel")), panel.el),
        stepsCard(["Every reference is measured: caption look, cuts, pace, music energy, hook timing.",
                   "Competitors are measured for hooks and structure (never reused).",
                   "Claude writes a beat plan with motion-graphics templates (or the offline planner does).",
                   "The plan becomes a timeline and renders locally with captions, transitions and sound.",
                   "The result is re-measured against the reference in QA.md."])));
    return {
      el, panels: [panel], primary: submit.run,
      refresh: () => [refs, comps, assets, voice, music].forEach(z => z.refresh()),
      onShow: () => {
        [refs, comps, assets, voice, music].forEach(z => z.refresh());
        const ho = S.handoff.reel;
        if (ho) { refs.set(ho.refs || []); delete S.handoff.reel; }
      },
    };
  }

  // ------------------------------------------------------------------ views: auto-edit

  function styleChoices() {
    return S.jobs.filter(j => j.kind === "extract" && j.status === "done")
      .map(j => ({ path: "jobs/" + j.id + "/out/style_dna.json", label: j.title }));
  }

  function viewAutoEdit() {
    const hint = () => "Edit " + (video.values()[0] ? basename(video.values()[0]).replace(/\.[^.]+$/, "") : "");
    const video = Dropzone({ label: "Raw video", accept: ["video"], noun: "your video", icon: "film",
      hint: "Talking head, podcast, vlog. Up to 4 GB.", projectHint: () => "Auto-edit" });
    const style = Dropzone({ label: "Style reference", accept: ["video", "json"], urls: true, noun: "a reel", icon: "scan", optional: true,
      hint: "A reel whose captions and pacing you want (file or link), or a Style Extract result.",
      extraChoices: styleChoices, extraLabel: "Style Extract results", projectHint: hint });
    const aspect = Seg("Aspect", [{ value: "source", label: "Source", icon: "aspect-src" }].concat(ASPECT_OPTS), "9:16");
    const energy = Seg("Energy", [{ value: "auto", label: "Auto" }, { value: "calm", label: "Calm" }, { value: "medium", label: "Medium" }, { value: "high", label: "High" }], "auto");
    const zoom = Toggle("Punch-in zooms", true, "On emphasis words");
    const graphics = Toggle("Motion graphics", true, "Hook title, numbers, lists, callouts");
    const sfx = Toggle("Sound design", true, "Whooshes and hits on cuts");
    const captions = Toggle("Captions", true, "Word-timed, in the reference style");
    const hook = h("input", { class: "input", maxlength: "200", placeholder: "e.g. I QUIT MY JOB FOR THIS" });
    const maxDur = h("input", { class: "input", type: "number", min: "3", max: "3600", step: "1", placeholder: "keep everything" });
    const broll = Dropzone({ label: "B-roll", multiple: true, accept: ["video", "image"], noun: "clips", icon: "image", optional: true,
      hint: "Cut in over the talking where it fits.", projectHint: hint });
    const music = Dropzone({ label: "Music", accept: ["audio"], noun: "a track", icon: "music", optional: true, hint: "Ducked under your voice.", projectHint: hint });
    const capFile = Dropzone({ label: "Captions file", accept: ["captions"], noun: "an .srt/.vtt", icon: "cc", optional: true,
      hint: "Skips transcription when you have one.", projectHint: hint });
    const quality = Seg("Quality", [{ value: "preview", label: "Preview (fast)" }, { value: "final", label: "Final" }], "final");

    const panel = JobPanel({ kinds: ["autoedit"], emptyIcon: "scissors", emptyTitle: "Your edit shows up here",
      emptyText: "Drop a raw video, pick a style and press Edit my video.",
      renderDone: async job => {
        const v = videoResult(job, ["edited.mp4"]);
        const spec = specOf(job, ["edit.json"]);
        const out = [];
        if (v) out.push(v.player.el);
        out.push(h("div", { class: "player-actions" },
          v ? h("a", { class: "btn sm", href: fileUrl(job.project, v.o.path, true) }, icon("download", 14), "Download") : null,
          spec ? h("button", { class: "btn sm", type: "button", onclick: () => go("timeline", { path: spec }) }, icon("layers", 14), "Open timeline") : null));
        const rep = await mdSection(job, "EDIT_REPORT.md", "Edit report: what was cut and why", !v);
        return out.concat(rep ? [rep] : []);
      } });

    const submit = SubmitButton("Edit my video", async () => {
      if (!video.value()) { toast("Add the raw video first", { kind: "info" }); return; }
      if (!needUploadsDone([video, style, broll, music, capFile])) return;
      const args = { video: video.value(), aspect: aspect.value(), energy: energy.value(), zoom: zoom.value(),
                     graphics: graphics.value(), sfx: sfx.value(), captions: captions.value(), broll: broll.values(),
                     render: true, preview: quality.value() === "preview" };
      if (style.value()) args.style = style.value();
      if (music.value()) args.music = music.value();
      if (capFile.value()) args.captions_file = capFile.value();
      if (hook.value.trim()) args.hook = hook.value.trim();
      if (maxDur.value) args.max_duration = parseFloat(maxDur.value);
      const job = await submitJob("autoedit", args, hint());
      panel.reset();
      updatePanels();
      if (job.status === "queued") toast("Queued", { kind: "info", message: "It starts when the current render finishes." });
    });

    const el = h("div", { class: "split fade-in" },
      h("div", { class: "stack" },
        h("section", { class: "card" }, h("div", { class: "card-head" }, h("h2", { class: "card-title" }, h("span", { class: "num", text: "1" }), "Footage")), video.el),
        h("section", { class: "card" }, h("div", { class: "card-head" }, h("h2", { class: "card-title" }, h("span", { class: "num", text: "2" }), "Look")),
          h("div", { class: "stack" }, style.el, Field("Aspect ratio", aspect.el, { hint: "16:9 footage is reframed to 9:16 by following the face." }),
            Field("Energy", energy.el, { hint: "How hard the cuts, zooms and graphics hit. Auto reads it from how fast you talk." }))),
        h("section", { class: "card" }, h("div", { class: "card-head" }, h("h2", { class: "card-title" }, h("span", { class: "num", text: "3" }), "Edit")),
          h("div", { class: "stack" }, h("div", { class: "toggles" }, zoom.el, graphics.el, sfx.el, captions.el),
            h("div", { class: "grid-2" }, Field("Hook title", hook, { optional: true, hint: "On screen in the first seconds." }),
              Field("Max length (s)", maxDur, { optional: true, hint: "Trim to the strongest part." })))),
        h("section", { class: "card" }, h("div", { class: "card-head" }, h("h2", { class: "card-title" }, h("span", { class: "num", text: "4" }), "Extras")),
          h("div", { class: "stack" }, broll.el, h("div", { class: "grid-2" }, music.el, capFile.el), Field("Quality", quality.el))),
        submit.el),
      h("div", { class: "stack sticky" },
        h("section", { class: "card" }, h("div", { class: "card-head" }, h("h2", { class: "card-title" }, icon("play", 15), "Latest edit")), panel.el),
        stepsCard(["Words are timed from captions, a file you give, or local transcription.",
                   "Dead air, filler words and retakes are cut; every cut is listed in the report.",
                   "16:9 is reframed to 9:16 by tracking the speaker's face.",
                   "Punch-ins land on emphasis, graphics on numbers, lists and questions.",
                   "Captions in the reference style, sound design, ducked music, then the render."])));
    return {
      el, panels: [panel], primary: submit.run,
      refresh: () => [video, style, broll, music, capFile].forEach(z => z.refresh()),
      onShow: () => {
        [video, style, broll, music, capFile].forEach(z => z.refresh());
        const ho = S.handoff.autoedit;
        if (ho) { style.clear(); style.set(ho.style); delete S.handoff.autoedit; toast("Style loaded", { kind: "ok", message: "The extracted style will be used for this edit." }); }
      },
    };
  }

  // ------------------------------------------------------------------ views: channel analysis

  function viewChannel() {
    const links = h("textarea", { class: "textarea", rows: "4", placeholder: "https://www.youtube.com/@creator\nhttps://www.instagram.com/creator/\nhttps://www.tiktok.com/@creator" });
    const chips = h("div", { class: "chips", style: { marginTop: "4px" } });
    const parse = () => links.value.split(/[\s,]+/).map(s => s.trim()).filter(Boolean);
    links.addEventListener("input", () => {
      clear(chips);
      parse().slice(0, 30).forEach(u => chips.appendChild(h("span", { class: "chip " + (looksLikeUrl(u) ? "ok" : "bad"), title: u },
        icon(looksLikeUrl(u) ? "check" : "x", 13), h("span", { class: "ellipsis", style: { maxWidth: "260px" }, text: u }))));
    });
    const maxVideos = h("input", { class: "input", type: "number", min: "1", max: "500", value: "30" });
    const deep = h("input", { class: "input", type: "number", min: "0", max: "30", value: "4" });
    const download = Toggle("Download thumbnails and top-video clips", true, "Needed for the thumbnail sheet and the deep dives.");

    const panel = JobPanel({ kinds: ["channel"], emptyIcon: "chart", emptyTitle: "No analysis yet",
      emptyText: "Paste one or more channel or profile links and press Analyze.",
      renderDone: async job => {
        const out = [];
        const sheet = findOutput(job, "thumbs_sheet.jpg");
        const md = findOutput(job, "CHANNEL_DNA.md");
        const tabs = h("div", { class: "tabs", role: "tablist" });
        const body = h("div");
        const pages = [];
        if (md) pages.push({ label: "Report", make: async () => markdown(await fetchText(job.project, md.path), mdResolver(job.project, md.path)) });
        if (sheet) pages.push({ label: "Thumbnails", make: async () => h("a", { href: fileUrl(job.project, sheet.path), target: "_blank" },
          h("img", { src: fileUrl(job.project, sheet.path), alt: "Thumbnail contact sheet", style: { borderRadius: "12px", border: "1px solid var(--line)" } })) });
        pages.push({ label: "Files", make: async () => {
          const viewer = h("div", { style: { marginBottom: "14px" } });
          return h("div", null, viewer, outputsGrid(job, async o => { clear(viewer); viewer.appendChild(await viewerFor(job, o)); }));
        } });
        const show = async (k) => {
          tabs.querySelectorAll(".tab").forEach((t, n) => t.setAttribute("aria-selected", n === k ? "true" : "false"));
          clear(body).appendChild(h("div", { class: "row muted small" }, h("span", { class: "spinner dark" }), "Loading..."));
          try { const node = await pages[k].make(); clear(body).appendChild(node); }
          catch (e) { clear(body).appendChild(h("div", { class: "callout bad" }, icon("xcircle"), h("div", { text: e.message }))); }
        };
        pages.forEach((p, k) => tabs.appendChild(h("button", { class: "tab", role: "tab", type: "button", onclick: () => show(k) }, p.label)));
        out.push(h("div", { class: "row" },
          md ? h("a", { class: "btn sm", href: fileUrl(job.project, md.path, true) }, icon("download", 14), "CHANNEL_DNA.md") : null,
          findOutput(job, "CHANNEL_DNA.json") ? h("a", { class: "btn sm", href: fileUrl(job.project, findOutput(job, "CHANNEL_DNA.json").path, true) }, icon("download", 14), "JSON") : null));
        out.push(tabs, body);
        show(0);
        return out;
      } });

    const submit = SubmitButton("Analyze", async () => {
      const urls = parse();
      if (!urls.length) { links.focus(); toast("Paste at least one link", { kind: "info" }); return; }
      const bad = urls.filter(u => !looksLikeUrl(u));
      if (bad.length) { toast("Some links look wrong", { kind: "bad", message: bad.join(", ") }); return; }
      await submitJob("channel", { urls, max_videos: num(maxVideos.value, 30), deep: num(deep.value, 4), download: download.value() },
        "Channel " + urls[0].replace(/^https?:\/\/(www\.)?/, "").slice(0, 40));
      panel.reset();
      updatePanels();
    });

    const el = h("div", { class: "stack fade-in" },
      h("section", { class: "card" },
        h("div", { class: "grid-2", style: { gridTemplateColumns: "minmax(0, 1.4fr) minmax(0, 1fr)", alignItems: "start" } },
          h("div", { class: "stack" }, Field("Channel and profile links", links, { hint: "YouTube channels, Instagram and TikTok profiles. One per line; several are compared." }), chips),
          h("div", { class: "stack" },
            h("div", { class: "grid-2" }, Field("Videos to read", maxVideos, { hint: "Per channel (long-form, Shorts, lives)." }),
              Field("Deep dives", deep, { hint: "Top videos analyzed for dialogue, energy and edit." })),
            download.el, submit.el))),
      h("section", { class: "card" }, h("div", { class: "card-head" }, h("h2", { class: "card-title" }, icon("chart", 16), "Channel DNA")), panel.el));
    return { el, panels: [panel], primary: submit.run };
  }

  // ------------------------------------------------------------------ views: style extract

  function viewStyle() {
    const src = Dropzone({ label: "Video to study", accept: ["video"], urls: true, noun: "a video", icon: "film",
      hint: "A reel, Short or TikTok whose look you want. File or link.", projectHint: () => "Style extract" });
    const capFile = Dropzone({ label: "Transcript", accept: ["captions"], noun: "an .srt/.vtt/.json3", icon: "cc", optional: true,
      hint: "Optional; YouTube captions or local transcription are tried otherwise." });
    const fps = h("input", { class: "input", type: "number", min: "0.5", max: "10", step: "0.5", value: "4" });
    const skipEdit = Toggle("Skip the edit analysis", false, "Faster; captions and dialogue only.");
    const whisper = Toggle("Transcribe locally when needed", true, "faster-whisper on this computer.");

    const panel = JobPanel({ kinds: ["extract"], emptyIcon: "scan", emptyTitle: "No style extracted yet",
      emptyText: "Give it one video. You get the caption font look, sizes, colours, pacing, hook and edit rhythm.",
      renderDone: async job => {
        const md = findOutput(job, "STYLE_DNA.md");
        const sheet = findOutput(job, "fonts_sheet.jpg");
        const dna = findOutput(job, "style_dna.json");
        const actions = h("div", { class: "row" },
          dna ? h("button", { class: "btn sm primary", type: "button", onclick: () => { S.handoff.autoedit = { style: dna.path }; go("autoedit"); } }, icon("scissors", 14), "Use in Auto-Edit") : null,
          h("button", { class: "btn sm", type: "button", onclick: () => { S.handoff.reel = { refs: [job.args && job.args.src].filter(Boolean) }; go("reel"); } }, icon("sparkles", 14), "Use as reel reference"),
          md ? h("a", { class: "btn sm", href: fileUrl(job.project, md.path, true) }, icon("download", 14), "STYLE_DNA.md") : null);
        const parts = [actions];
        if (sheet) parts.push(h("div", null, h("div", { class: "field-label", style: { marginBottom: "8px" } }, h("span", null, "On-screen text crops (name the font by eye)")),
          h("a", { href: fileUrl(job.project, sheet.path), target: "_blank" }, h("img", { src: fileUrl(job.project, sheet.path), alt: "Font sheet", style: { borderRadius: "12px", border: "1px solid var(--line)" } }))));
        if (md) parts.push(markdown(await fetchText(job.project, md.path), mdResolver(job.project, md.path)));
        return parts;
      } });

    const submit = SubmitButton("Extract the style", async () => {
      if (!src.value()) { toast("Add a video or a link first", { kind: "info" }); return; }
      if (!needUploadsDone([src, capFile])) return;
      const args = { src: src.value(), fps: num(fps.value, 4), skip_edit: skipEdit.value(), whisper: whisper.value() };
      if (capFile.value()) args.captions = capFile.value();
      await submitJob("extract", args, "Style " + basename(src.value()).slice(0, 40));
      panel.reset();
      updatePanels();
    });

    const el = h("div", { class: "split fade-in" },
      h("div", { class: "stack" },
        h("section", { class: "card" }, h("div", { class: "stack" }, src.el, capFile.el,
          h("div", { class: "grid-2" }, Field("Text samples per second", fps, { hint: "Higher reads fast captions better; slower." }), h("div")),
          h("div", { class: "toggles" }, skipEdit.el, whisper.el))),
        submit.el,
        stepsCard(["Every on-screen text card is read with OCR and measured: weight, width, case, size, position, colours, outline, box.",
                   "Free look-alike fonts are proposed; the crop sheet lets you name the font by eye.",
                   "Dialogue: words per minute, sentence rhythm, pauses, hook, re-hooks and CTA.",
                   "Edit: shot lengths, cuts on the beat, BPM, camera moves and colour."])),
      h("div", { class: "stack" }, h("section", { class: "card" }, h("div", { class: "card-head" }, h("h2", { class: "card-title" }, icon("scan", 16), "Style DNA")), panel.el)));
    return { el, panels: [panel], primary: submit.run, refresh: () => [src, capFile].forEach(z => z.refresh()), onShow: () => [src, capFile].forEach(z => z.refresh()) };
  }

  // ------------------------------------------------------------------ views: script

  const FORMATS = ["storytime", "tutorial", "list", "pov", "mistakes-myths", "versus", "challenge", "day-in-life",
                   "before-after", "reaction", "explainer", "review", "hot-take", "skit", "behind-scenes"];

  function scoreCard(score) {
    const overall = Math.round(score.overall || 0);
    const ring = h("div", { class: "ring", role: "img", "aria-label": "scriptcheck score " + overall },
      h("div", { class: "ring-val" }, h("b", { text: String(overall) }), h("span", { text: score.verdict || "" })));
    ring.style.setProperty("--p", overall);
    ring.style.setProperty("--c", overall >= 80 ? "var(--ok)" : overall >= 60 ? "var(--warn)" : "var(--bad)");
    const rows = Object.entries(score.checks || {}).map(([name, c]) => {
      const v = c.score;
      const fill = h("i", { class: v === null ? "" : v < 50 ? "low" : v < 75 ? "mid" : "high" });
      fill.style.width = (v === null ? 0 : v) + "%";
      return h("div", { class: "check-row", title: c.detail || "" }, h("span", { class: "c-name", text: name }),
        h("div", { class: "bar" }, fill), h("span", { class: "c-val", text: v === null ? "-" : String(v) }));
    });
    const extra = [];
    if (score.human) extra.push(h("span", { class: "pill plain", text: "human check " + score.human.human_score + " · " + score.human.verdict }));
    if (score.seconds) extra.push(h("span", { class: "pill plain", text: "~" + score.seconds + " s at " + score.wpm + " wpm" }));
    if (score.angle) extra.push(h("span", { class: "pill plain", text: score.angle }));
    return h("div", { class: "stack" }, h("div", { class: "score" }, ring, h("div", { class: "checks" }, rows)),
      extra.length ? h("div", { class: "row", style: { gap: "6px" } }, extra) : null);
  }

  function viewScript() {
    const topic = h("textarea", { class: "textarea big", rows: "3", maxlength: "2000",
      placeholder: "Why I stopped posting every day, and what happened to my views" });
    const notes = h("textarea", { class: "textarea", rows: "4", maxlength: "6000",
      placeholder: "Real facts only you know: numbers, names, dates, what happened. Anything missing becomes a [PLACEHOLDER], never a made-up number." });
    const seconds = Range(10, 90, 1, 35, " s");
    const wpm = h("input", { class: "input", type: "number", min: "100", max: "280", value: "170" });
    const format = h("select", { class: "select" }, h("option", { value: "", text: "Any format" }), FORMATS.map(f => h("option", { value: f, text: f.replace(/-/g, " ") })));
    const style = h("select", { class: "select" });
    const refreshStyles = () => {
      const keep = style.value;
      clear(style).appendChild(h("option", { value: "", text: "No reference" }));
      styleChoices().forEach(c => style.appendChild(h("option", { value: c.path, text: c.label })));
      style.value = keep;
    };
    refreshStyles();

    const panel = JobPanel({ kinds: ["script"], emptyIcon: "pen", emptyTitle: "No script yet",
      emptyText: "Give it a topic and the real facts. It writes three angles, scores them as speech and fixes the weakest beats.",
      renderDone: async job => {
        const md = findOutput(job, "script.md");
        const sc = findOutput(job, "score.json");
        const parts = [];
        if (sc) { try { parts.push(scoreCard(JSON.parse(await fetchText(job.project, sc.path)))); } catch (e) { /* no score */ } }
        if (md) {
          const text = await fetchText(job.project, md.path);
          parts.push(h("div", { class: "row" },
            h("button", { class: "btn sm", type: "button", onclick: async () => {
              const spoken = [];
              text.split("\n").forEach(l => { const c = l.split(/(?<!\\)\|/); if (/^\|\s*\d+:\d\d-/.test(l) && c.length > 2) spoken.push(c[2].trim().replace(/\\\|/g, "|")); });
              try { await navigator.clipboard.writeText(spoken.join("\n")); toast("Spoken lines copied", { kind: "ok" }); }
              catch (e) { fail(e, "Could not copy"); }
            } }, icon("copy", 14), "Copy spoken lines"),
            h("a", { class: "btn sm", href: fileUrl(job.project, md.path, true) }, icon("download", 14), "script.md")));
          parts.push(markdown(text, mdResolver(job.project, md.path)));
        }
        return parts;
      } });

    const submit = SubmitButton("Write the script", async () => {
      if (!topic.value.trim()) { topic.focus(); toast("What is the script about?", { kind: "info" }); return; }
      const args = { topic: topic.value.trim(), seconds: seconds.value(), wpm: num(wpm.value, 170) };
      if (notes.value.trim()) args.notes = notes.value.trim();
      if (format.value) args.format = format.value;
      if (style.value) args.style = style.value;
      await submitJob("script", args, "Script " + topic.value.trim().split(/\s+/).slice(0, 5).join(" "));
      panel.reset();
      updatePanels();
    });

    const el = h("div", { class: "split fade-in" },
      h("div", { class: "stack" },
        h("section", { class: "card" }, h("div", { class: "stack" },
          Field("Topic", topic), Field("The real facts", notes, { optional: true }),
          h("div", { class: "grid-2" }, Field("Length", seconds.el), Field("Speaking pace (wpm)", wpm, { hint: "A reference's pace replaces 170." })),
          h("div", { class: "grid-2" }, Field("Format", format), Field("Pace of a reference", style, { hint: "From Style Extract results." })))),
        h("div", { class: "callout info" }, icon("info"), h("div", { text: "Writes with your Claude subscription through Claude Code. Scored with script-forge's scriptcheck (80+ and no red check ships). The simulated viewer panel runs in Claude Code with /script-forge." })),
        submit.el),
      h("div", { class: "stack sticky" }, h("section", { class: "card" }, h("div", { class: "card-head" }, h("h2", { class: "card-title" }, icon("pen", 16), "Script")), panel.el)));
    return { el, panels: [panel], primary: submit.run, refresh: refreshStyles, onShow: refreshStyles };
  }

  // ------------------------------------------------------------------ views: jobs

  function viewJobs() {
    const el = h("div", { class: "jobs-layout fade-in" });
    const listBox = h("div", { class: "job-list", role: "list" });
    const detail = h("div", { class: "stack" });
    let filter = store.get("jobs.filter", "all");
    let scope = store.get("jobs.scope", "project");
    let all = [];
    let selected = S.params.job || null;
    let log = null, shownSig = "";

    const filters = [["all", "All"], ["active", "Active"], ["done", "Done"], ["failed", "Failed"]];
    const filterRow = h("div", { class: "chips" }, filters.map(([k, label]) =>
      h("button", { class: "chip chip-filter", type: "button", "aria-pressed": filter === k ? "true" : "false",
                    onclick: e => { filter = k; store.set("jobs.filter", k); filterRow.querySelectorAll("button").forEach(b => b.setAttribute("aria-pressed", "false")); e.currentTarget.setAttribute("aria-pressed", "true"); paintList(); } }, label)));
    const scopeSel = h("select", { class: "select sm jobs-scope", "aria-label": "Which jobs" },
      h("option", { value: "project", text: "This project" }), h("option", { value: "all", text: "All projects" }));
    scopeSel.value = scope;
    scopeSel.addEventListener("change", () => { scope = scopeSel.value; store.set("jobs.scope", scope); load(); });

    async function load() {
      try {
        all = scope === "all" || !S.pid ? await api("GET", "/api/jobs") : S.jobs;
      } catch (e) { all = []; }
      paintList();
      if (!selected && all.length) selected = all[0].id;
      paintDetail();
    }

    function visible() {
      return all.filter(j => filter === "all" || (filter === "active" ? ACTIVE(j.status) : filter === "done" ? j.status === "done" : j.status === "failed" || j.status === "cancelled"));
    }

    function paintList() {
      clear(listBox);
      const list = visible();
      if (!list.length) {
        listBox.appendChild(h("div", { class: "empty" }, h("div", { class: "empty-art" }, icon("pulse", 24)), h("h3", { text: "Nothing here" }),
          h("p", { text: all.length ? "No jobs match this filter." : "Jobs appear when you make a reel, edit, analyze or render." })));
        return;
      }
      for (const j of list) {
        const fill = h("i");
        fill.style.width = (j.status === "done" ? 100 : j.progress ? Math.round(j.progress * 100) : 0) + "%";
        listBox.appendChild(h("button", { class: "job-item", type: "button", role: "listitem", "aria-current": j.id === selected ? "true" : "false",
                                          onclick: () => { selected = j.id; history.replaceState(null, "", "#/jobs?job=" + j.id); paintList(); paintDetail(); } },
          h("div", { class: "ji-top" }, h("span", { class: "pill kind plain", text: KIND_LABEL[j.kind] || j.kind }), h("span", { class: "ji-title", text: j.title }), statusPill(j)),
          ACTIVE(j.status) ? h("div", { class: "bar" + (j.progress === null && j.status === "running" ? " indeterminate" : "") }, fill) : null,
          h("div", { class: "ji-meta" }, h("span", { text: fmtAgo(j.created) }), j.elapsed ? h("span", { text: "· " + fmtDur(j.elapsed) }) : null,
            scope === "all" ? h("span", { class: "ellipsis", text: "· " + j.project }) : null)));
      }
    }

    async function paintDetail(force) {
      const j = all.find(x => x.id === selected);
      if (!j) {
        if (log) { log.stop(); log = null; }
        clear(detail).appendChild(h("section", { class: "card" }, h("div", { class: "empty" }, h("div", { class: "empty-art" }, icon("pulse", 24)),
          h("h3", { text: "Pick a job" }), h("p", { text: "Its live log, progress and output files show here." }))));
        shownSig = "";
        return;
      }
      const sig = j.id + "|" + j.status;
      if (sig === shownSig && !force) {
        const box = detail.querySelector(".running-detail");
        if (box && ACTIVE(j.status)) box.replaceWith(runningDetail(j));
        return;
      }
      shownSig = sig;
      if (log) { log.stop(); log = null; }
      let d;
      try { d = await jobDetail(j.id, true); } catch (e) { clear(detail).appendChild(h("div", { class: "callout bad" }, icon("xcircle"), h("div", { text: e.message }))); return; }
      if (shownSig !== sig) return;
      clear(detail);
      const viewer = h("div");
      const openOutput = async o => {
        clear(viewer).appendChild(h("div", { class: "row muted small" }, h("span", { class: "spinner dark" }), "Opening " + o.name + "..."));
        try { const node = await viewerFor(d, o); clear(viewer).appendChild(h("section", { class: "card" }, h("div", { class: "card-head" }, h("h2", { class: "card-title", text: o.name }),
          h("a", { class: "btn xs", href: fileUrl(d.project, o.path, true) }, icon("download", 13), "Download")), node)); }
        catch (e) { clear(viewer).appendChild(h("div", { class: "callout bad" }, icon("xcircle"), h("div", { text: e.message }))); }
      };
      const spec = (d.outputs || []).find(o => /(^|\/)(spec|edit)(\.v\d+)?\.json$/.test(o.path) && o.path.split("/").length <= 4);
      detail.appendChild(h("section", { class: "card" },
        h("div", { class: "job-head" }, h("div", { class: "grow", style: { minWidth: 0 } },
          h("div", { class: "row", style: { gap: "8px" } }, h("span", { class: "pill kind plain", text: KIND_LABEL[d.kind] || d.kind }), statusPill(d)),
          h("h2", { class: "job-title", style: { fontSize: "17px", marginTop: "8px" }, text: d.title }),
          h("div", { class: "muted small", text: "Started " + (d.started ? new Date(d.started * 1000).toLocaleString() : "not yet") + (d.elapsed ? " · took " + fmtDur(d.elapsed) : "") + (d.exit_code !== null && d.exit_code !== undefined ? " · exit " + d.exit_code : "") })),
          h("div", { class: "row" },
            ACTIVE(d.status) ? h("button", { class: "btn sm danger", type: "button", onclick: () => cancelJob(d.id) }, icon("stop", 13), "Cancel") : null,
            !ACTIVE(d.status) ? h("button", { class: "btn sm", type: "button", onclick: () => rerun(d) }, icon("refresh", 14), "Run again") : null,
            spec ? h("button", { class: "btn sm", type: "button", onclick: () => go("timeline", { path: spec.path }) }, icon("layers", 14), "Timeline") : null,
            KIND_VIEW[d.kind] && !["timeline", "templates"].includes(KIND_VIEW[d.kind]) && d.project === S.pid ? h("button", { class: "btn sm", type: "button", onclick: () => go(KIND_VIEW[d.kind]) }, icon("arrow", 14), "Open") : null)),
        ACTIVE(d.status) ? runningDetail(j) : null,
        d.status === "failed" ? h("div", { class: "callout bad", style: { marginTop: "14px" } }, icon("xcircle"), h("div", { text: d.error || "Failed; see the log." })) : null,
        (d.notes || []).length ? h("div", { class: "callout warn", style: { marginTop: "14px" } }, icon("warn"), h("div", { text: d.notes.join(" ") })) : null,
        h("details", { style: { marginTop: "14px" } }, h("summary", { class: "muted small", style: { cursor: "pointer" }, text: "Command" }),
          h("code", { class: "code-line", style: { marginTop: "8px" }, text: d.command || "" }))));
      log = LogView(d.id, () => { const x = all.find(y => y.id === d.id); return x && ACTIVE(x.status); });
      detail.appendChild(h("section", { class: "card" }, h("div", { class: "card-head" }, h("h2", { class: "card-title" }, icon("pulse", 15), "Log"),
        h("a", { class: "btn xs", href: fileUrl(d.project, "jobs/" + d.id + "/log.txt", true) }, icon("download", 13), "log.txt")), log.el));
      detail.appendChild(h("section", { class: "card" }, h("div", { class: "card-head" }, h("h2", { class: "card-title" }, icon("folder", 15), "Outputs"),
        h("span", { class: "muted small", text: (d.outputs || []).length + " file(s) in jobs/" + d.id + "/out" })), outputsGrid(d, openOutput)));
      detail.appendChild(viewer);
      const primary = d.primary && (d.outputs || []).find(o => o.path === d.primary);
      if (primary && d.status === "done" && ["video", "image", "markdown"].includes(primary.type)) openOutput(primary);
    }

    function runningDetail(j) {
      return h("div", { class: "running-detail stack", style: { gap: "8px", marginTop: "14px" } }, progressLine(j),
        h("div", { class: "job-stage", text: j.status === "queued" ? "Waiting for a free render slot..." : (j.stage || "Starting...") }));
    }

    const panel = { update: () => { if (scope !== "all" || !S.pid) { all = S.jobs; paintList(); paintDetail(); } else load(); } };
    el.appendChild(h("div", { class: "stack" }, h("div", { class: "row-between" }, filterRow, scopeSel), listBox));
    el.appendChild(detail);
    load();
    return { el, panels: [panel], actions: [h("button", { class: "btn sm", type: "button", onclick: () => load() }, icon("refresh", 15), "Refresh")] };
  }

  // ------------------------------------------------------------------ views: timeline

  const ENTERS = ["none", "fade", "pop", "slide-up", "slide-down", "slide-left", "slide-right", "zoom-in", "zoom-out", "drop", "bounce",
                  "stamp", "blur-in", "wipe-right", "wipe-left", "wipe-up", "typewriter", "words", "crossfade", "whip-left", "whip-right", "glitch"];
  const EXITS = ["none", "fade", "pop-out", "slide-up", "slide-down", "slide-left", "slide-right", "zoom-in", "zoom-out", "blur-out",
                 "wipe-right", "wipe-left", "whip-left", "whip-right", "glitch"];
  const LAYER_ICON = { video: "film", image: "image", text: "text", counter: "hash", shape: "shape", progress: "bar" };

  function specDuration(spec) {
    if (num(spec.duration, 0) > 0) return spec.duration;
    let d = 0;
    (spec.layers || []).forEach(L => { d = Math.max(d, num(L.end, 0), num(L.start, 0)); });
    (spec.audio || []).forEach(a => { d = Math.max(d, num(a.start, 0) + num(a.duration, 0)); });
    const words = (spec.captions && spec.captions.words) || [];
    if (words.length) d = Math.max(d, num(words[words.length - 1].e, 0));
    return d || 10;
  }

  function layerLabel(L) {
    const id = L.id ? L.id + ": " : "";
    if (L.type === "text") return id + "“" + String(L.text || "").replace(/\s+/g, " ").slice(0, 60) + "”";
    if (L.type === "counter") return id + (L.prefix || "") + (L.to !== undefined ? L.to : "") + (L.suffix || "");
    if (L.type === "video" || L.type === "image") return id + basename(L.src || L.type);
    if (L.type === "shape") return id + (L.shape || "shape");
    return id + (L.type || "layer");
  }

  function viewTimeline() {
    const T = { path: S.params.path || store.get("timeline.path." + S.pid, null), data: null, spec: null, dirty: false, sel: -1, playhead: 0, tab: "layers" };
    const el = h("div", { class: "fade-in" });
    const toolbar = h("div", { class: "tl-toolbar" });
    const body = h("div");
    el.append(toolbar, body);
    let player = null, renderWatch = null, renderJobId = null;

    const specs = () => ((S.project && S.project.specs) || []);

    function paintToolbar() {
      clear(toolbar);
      const sel = h("select", { class: "select", "aria-label": "Timeline to edit" });
      const list = specs();
      if (!list.length) sel.appendChild(h("option", { value: "", text: "No timelines in this project yet" }));
      const groups = { spec: [], plan: [] };
      list.forEach(s => groups[s.kind].push(s));
      if (groups.spec.length) sel.appendChild(h("optgroup", { label: "Timelines" }, groups.spec.map(s => h("option", { value: s.path, text: s.path }))));
      if (groups.plan.length) sel.appendChild(h("optgroup", { label: "Plans (compile to edit)" }, groups.plan.map(s => h("option", { value: s.path, text: s.path }))));
      sel.value = T.path || "";
      sel.addEventListener("change", () => {
        if (T.dirty && !confirm("Discard unsaved changes?")) { sel.value = T.path; return; }
        open(sel.value);
      });
      toolbar.appendChild(sel);
      if (T.data && T.data.versions && T.data.versions.length > 1) {
        toolbar.appendChild(h("div", { class: "versions", role: "group", "aria-label": "Versions" }, T.data.versions.map(v =>
          h("button", { type: "button", "aria-pressed": v.path === T.data.path ? "true" : "false", title: v.path,
                        onclick: () => { if (T.dirty && !confirm("Discard unsaved changes?")) return; open(v.path); } }, "v" + v.version))));
      }
      if (T.dirty) toolbar.appendChild(h("span", { class: "dirty", text: "Unsaved" }));
      toolbar.appendChild(h("span", { class: "grow" }));
      if (T.data && T.data.kind === "spec") {
        add(toolbar, [
          h("button", { class: "btn sm", type: "button", disabled: !T.dirty, onclick: () => save(false) }, icon("save", 14), "Save"),
          h("button", { class: "btn sm", type: "button", onclick: () => save(true) }, icon("copy", 14), "Save as new version"),
          h("button", { class: "btn sm", type: "button", onclick: () => renderSpec({ preview: true }) }, icon("play", 13), "Render preview"),
          h("button", { class: "btn sm primary", type: "button", onclick: () => renderSpec({}) }, icon("bolt", 14), "Render final"),
        ]);
      } else if (T.data && T.data.kind === "plan") {
        toolbar.appendChild(h("button", { class: "btn sm primary", type: "button", onclick: async () => {
          try { await submitJob("plan", { plan: T.data.path }); toast("Compiling the plan", { kind: "info", message: "The timeline appears in the list when it's done." }); }
          catch (e) { fail(e, "Could not compile"); }
        } }, icon("bolt", 14), "Compile to timeline"));
      }
    }

    async function open(path) {
      T.path = path || null;
      T.dirty = false;
      T.sel = -1;
      store.set("timeline.path." + S.pid, T.path);
      if (path) history.replaceState(null, "", "#/timeline?path=" + encodeURIComponent(path));
      if (!path) { T.data = null; T.spec = null; paint(); return; }
      clear(body).appendChild(h("div", { class: "row muted" }, h("span", { class: "spinner dark" }), "Opening " + path + "..."));
      try {
        T.data = await api("GET", "/api/projects/" + encodeURIComponent(S.pid) + "/spec?path=" + encodeURIComponent(path));
        T.path = T.data.path;
        T.spec = clone(T.data.spec);
      } catch (e) {
        T.data = null; T.spec = null;
        clear(body).appendChild(h("div", { class: "callout bad" }, icon("xcircle"), h("div", { text: e.message })));
        paintToolbar();
        return;
      }
      paint();
    }

    function markDirty() {
      if (!T.dirty) { T.dirty = true; paintToolbar(); }
    }

    async function save(asNew, force) {
      if (!T.spec) return null;
      const url = "/api/projects/" + encodeURIComponent(S.pid) + "/spec?path=" + encodeURIComponent(T.path) + (asNew ? "&version=new" : "") + (force ? "&force=1" : "");
      try {
        const r = await api("PUT", url, T.spec);
        T.dirty = false;
        toast(asNew ? "Saved as " + basename(r.path) : "Saved", { kind: "ok" });
        await loadProject();
        await open(r.path);
        return r.path;
      } catch (e) {
        if (e.status === 422 && e.data && e.data.errors) {
          showErrors(e.data.errors, () => save(asNew, true));
          return null;
        }
        fail(e, "Could not save");
        return null;
      }
    }

    function showErrors(errors, saveAnyway) {
      const box = body.querySelector(".tl-errors");
      if (!box) return;
      clear(box).appendChild(h("div", { class: "callout bad" }, icon("xcircle"),
        h("div", { class: "grow" }, h("b", { text: errors.length + " problem(s) in this timeline" }),
          h("ul", { class: "errors" }, errors.slice(0, 50).map(e => h("li", { text: e }))),
          saveAnyway ? h("button", { class: "btn xs", type: "button", style: { marginTop: "8px" }, onclick: () => { clear(box); saveAnyway(); } }, "Save anyway") : null)));
    }

    async function renderSpec(opts) {
      let path = T.path;
      if (T.dirty) { path = await save(false); if (!path) return; }
      try {
        const job = await submitJob("render", Object.assign({ spec: path }, opts));
        renderJobId = job.id;
        toast(opts.still !== undefined ? "Rendering a still" : opts.preview ? "Rendering a preview" : "Rendering full quality", { kind: "info", message: job.status === "queued" ? "Queued behind another render." : "Progress shows under the player." });
        followRender(job.id);
      } catch (e) { fail(e, "Could not start the render"); }
    }

    function followRender(id) {
      if (renderWatch) renderWatch();
      const status = body.querySelector(".tl-render-status");
      renderWatch = watchJob(id, job => {
        if (!status || !job) return;
        clear(status);
        if (ACTIVE(job.status)) {
          status.appendChild(h("div", { class: "stack", style: { gap: "6px" } }, progressLine(job),
            h("div", { class: "row-between" }, h("span", { class: "job-stage grow", text: job.status === "queued" ? "Waiting for a render slot..." : job.stage || "Rendering..." }),
              h("button", { class: "btn xs danger", type: "button", onclick: () => cancelJob(job.id) }, "Cancel"))));
        } else if (job.status === "done" && job.primary) {
          const isStill = /\.png$/i.test(job.primary);
          const preview = body.querySelector(".tl-preview");
          if (preview) {
            clear(preview);
            if (isStill) preview.appendChild(h("img", { class: "player portrait", src: fileUrl(job.project, job.primary) + "?t=" + Date.now(), alt: "Still frame" }));
            else { player = playerFor(job.project, job.primary, (T.spec.width || 9) > (T.spec.height || 16) ? "16:9" : "9:16"); preview.appendChild(player.el); hookPlayer(); }
          }
          status.appendChild(h("div", { class: "row-between small" }, h("span", { class: "pill done", text: isStill ? "Still ready" : "Render ready" }),
            h("a", { class: "btn xs", href: fileUrl(job.project, job.primary, true) }, icon("download", 13), "Download")));
        } else if (job.status === "failed") {
          status.appendChild(h("div", { class: "callout bad" }, icon("xcircle"), h("div", null, job.error || "The render failed. ",
            h("a", { href: "#/jobs?job=" + job.id, text: " See the log." }))));
        }
      });
    }

    function hookPlayer() {
      if (!player) return;
      player.video.addEventListener("timeupdate", () => setPlayhead(player.video.currentTime, false));
    }

    function setPlayhead(t, seek) {
      const dur = specDuration(T.spec);
      T.playhead = Math.max(0, Math.min(dur, t));
      const ph = body.querySelector(".playhead");
      const ruler = body.querySelector(".ruler");
      if (ph && ruler) ph.style.left = (ruler.offsetLeft + ruler.clientWidth * (T.playhead / dur)) + "px";
      const lab = body.querySelector(".tl-playhead-label");
      if (lab) lab.textContent = fmtClock(T.playhead);
      if (seek && player && player.video.readyState > 0) player.video.currentTime = T.playhead;
    }

    function paint() {
      paintToolbar();
      clear(body);
      if (!S.pid) {
        body.appendChild(h("section", { class: "card" }, h("div", { class: "empty" }, h("div", { class: "empty-art" }, icon("layers", 26)),
          h("h3", { text: "Pick a project first" }), h("p", { text: "Timelines come from reels, auto-edits and compiled plans." }))));
        return;
      }
      if (!T.data) {
        body.appendChild(h("section", { class: "card" }, h("div", { class: "empty" }, h("div", { class: "empty-art" }, icon("layers", 26)),
          h("h3", { text: specs().length ? "Choose a timeline above" : "No timelines yet" }),
          h("p", { text: "Every reel and auto-edit writes a timeline (spec.json / edit.json). Open one to change text, timing and layers, or ask Claude." }),
          h("div", { class: "row", style: { justifyContent: "center" } }, h("a", { class: "btn sm", href: "#/reel" }, icon("sparkles", 14), "Make a reel"),
            h("a", { class: "btn sm", href: "#/autoedit" }, icon("scissors", 14), "Auto-edit")))));
        return;
      }
      if (T.data.kind !== "spec") {
        body.appendChild(h("section", { class: "card" }, h("div", { class: "callout info" }, icon("info"),
          h("div", { text: T.data.kind === "plan" ? "This is a plan (beats). Compile it to a timeline to edit and render it." : "This JSON file is not a timeline." })),
          h("pre", { class: "log", style: { marginTop: "14px" }, text: JSON.stringify(T.data.spec, null, 2) })));
        return;
      }
      const left = h("div", { class: "tl-left" });
      const right = h("div", { class: "stack" });
      body.appendChild(h("div", { class: "tl-layout" }, left, right));

      // preview + render status
      const preview = h("div", { class: "tl-preview" });
      const latest = (T.data.renders || [])[0];
      const portrait = (T.spec.height || 1920) >= (T.spec.width || 1080);
      if (latest) { player = playerFor(S.pid, latest.path, portrait ? "9:16" : "16:9"); preview.appendChild(player.el); hookPlayer(); }
      else preview.appendChild(h("div", { class: "player placeholder-player " + (portrait ? "portrait" : "landscape") },
        h("div", null, icon("film", 30), h("p", { text: "No render of this version yet." }),
          h("button", { class: "btn sm primary", type: "button", onclick: () => renderSpec({ preview: true }) }, icon("play", 13), "Render preview"))));
      const stillBtn = h("button", { class: "btn xs", type: "button", onclick: () => renderSpec({ still: Math.round(T.playhead * 100) / 100 }) },
        icon("camera", 13), "Still at ", h("span", { class: "tl-playhead-label", text: fmtClock(T.playhead) }));
      left.appendChild(h("section", { class: "card", style: { padding: "14px" } }, preview,
        h("div", { class: "row-between", style: { marginTop: "12px" } },
          h("span", { class: "muted small", text: (T.spec.width || "?") + "×" + (T.spec.height || "?") + " · " + (T.spec.fps || 30) + " fps · " + specDuration(T.spec).toFixed(1) + " s" }),
          stillBtn),
        h("div", { class: "tl-render-status", style: { marginTop: "10px" } })));
      left.appendChild(askCard());
      const running = S.jobs.find(j => j.kind === "render" && ACTIVE(j.status) && j.args && j.args.spec === T.path);
      if (running) followRender(running.id);

      // editor
      const tabs = h("div", { class: "tabs", role: "tablist" });
      const pane = h("div");
      const tabDefs = [["layers", "Layers"], ["captions", "Captions & audio"], ["json", "JSON"]];
      tabDefs.forEach(([k, label]) => tabs.appendChild(h("button", { class: "tab", role: "tab", type: "button", "aria-selected": T.tab === k ? "true" : "false",
        onclick: () => { T.tab = k; tabs.querySelectorAll(".tab").forEach(b => b.setAttribute("aria-selected", b.textContent === label ? "true" : "false")); paintPane(pane); } }, label)));
      const errBox = h("div", { class: "tl-errors" });
      right.appendChild(h("section", { class: "card" }, tabs, errBox, pane));
      if (T.data.errors && T.data.errors.length) showErrors(T.data.errors, null);
      paintPane(pane);
    }

    function paintPane(pane) {
      clear(pane);
      if (T.tab === "layers") paintLayers(pane);
      else if (T.tab === "captions") paintCaptions(pane);
      else paintJson(pane);
    }

    function paintLayers(pane) {
      const layers = T.spec.layers || [];
      const dur = specDuration(T.spec);
      const ruler = h("div", { class: "ruler", title: "Click to move the playhead" });
      const step = dur <= 10 ? 1 : dur <= 30 ? 2 : dur <= 60 ? 5 : dur <= 180 ? 10 : 30;
      for (let t = 0; t <= dur + 1e-6; t += step) {
        const tick = h("div", { class: "tick" }, h("span", { text: t + "s" }));
        tick.style.left = (t / dur * 100) + "%";
        ruler.appendChild(tick);
      }
      ruler.addEventListener("click", e => {
        const r = ruler.getBoundingClientRect();
        setPlayhead((e.clientX - r.left) / r.width * dur, true);
      });
      const rows = h("div", { class: "track-rows", role: "listbox", "aria-label": "Layers" });
      layers.forEach((L, i) => {
        const start = num(L.start, 0), end = num(L.end, dur);
        const clip = h("div", { class: "clip ly-" + (L.type || "shape"), text: layerLabel(L) });
        clip.style.left = (start / dur * 100) + "%";
        clip.style.width = Math.max(0.4, (end - start) / dur * 100) + "%";
        const row = h("div", { class: "track", role: "option", tabindex: "0", "aria-selected": i === T.sel ? "true" : "false",
                               onclick: () => selectLayer(i, pane),
                               onkeydown: e => {
                                 if (e.key === "Enter" || e.key === " ") { e.preventDefault(); selectLayer(i, pane); }
                                 else if (e.key === "ArrowDown" && row.nextSibling) { e.preventDefault(); row.nextSibling.focus(); }
                                 else if (e.key === "ArrowUp" && row.previousSibling) { e.preventDefault(); row.previousSibling.focus(); }
                               } },
          h("div", { class: "track-label", title: layerLabel(L) }, h("span", { class: "t-type ly-" + (L.type || "shape") }, icon(LAYER_ICON[L.type] || "shape", 12)),
            h("span", { class: "t-name", text: layerLabel(L) })),
          h("div", { class: "track-lane" }, clip));
        rows.appendChild(row);
      });
      const playhead = h("div", { class: "playhead" });
      const tracks = h("div", { class: "tracks" }, ruler, rows, playhead);
      if (window.ResizeObserver) new ResizeObserver(() => setPlayhead(T.playhead, false)).observe(ruler);
      pane.appendChild(layers.length ? tracks : h("p", { class: "muted", text: "This timeline has no layers." }));
      setPlayhead(T.playhead, false);
      const insp = h("div", { class: "tl-inspector", style: { marginTop: "16px" } });
      pane.appendChild(insp);
      if (T.sel >= 0 && layers[T.sel]) paintInspector(insp, pane);
      else insp.appendChild(h("p", { class: "muted small", text: layers.length ? "Select a layer to edit its text, timing and look." : "" }));
    }

    function selectLayer(i, pane) {
      T.sel = i;
      const L = T.spec.layers[i];
      if (L) setPlayhead(num(L.start, 0) + 0.01, true);
      paintPane(pane);
      const row = pane.querySelectorAll(".track")[i];
      if (row) row.focus();
    }

    function paintInspector(box, pane) {
      const L = T.spec.layers[T.sel];
      const set = (key, value) => {
        if (value === "" || value === null || (typeof value === "number" && isNaN(value))) delete L[key]; else L[key] = value;
        markDirty();
        const row = pane.querySelectorAll(".track")[T.sel];
        if (row) {
          const dur = specDuration(T.spec);
          const clip = row.querySelector(".clip");
          const start = num(L.start, 0), end = num(L.end, dur);
          clip.style.left = (start / dur * 100) + "%";
          clip.style.width = Math.max(0.4, (end - start) / dur * 100) + "%";
          clip.textContent = layerLabel(L);
          row.querySelector(".t-name").textContent = layerLabel(L);
        }
      };
      const numInput = (key, step, placeholder) => {
        const inp = h("input", { class: "input sm", type: "number", step: step || "0.05", value: L[key] !== undefined && typeof L[key] !== "string" ? L[key] : "", placeholder: placeholder || "" });
        inp.addEventListener("change", () => set(key, inp.value === "" ? "" : parseFloat(inp.value)));
        return inp;
      };
      const posInput = key => {
        const inp = h("input", { class: "input sm", value: L[key] !== undefined ? L[key] : "", placeholder: "centre" });
        inp.addEventListener("change", () => { const v = inp.value.trim(); set(key, v === "" ? "" : /%$/.test(v) ? v : parseFloat(v)); });
        return inp;
      };
      const textInput = (key, placeholder) => {
        const inp = h("input", { class: "input sm", value: L[key] !== undefined ? L[key] : "", placeholder: placeholder || "" });
        inp.addEventListener("change", () => set(key, inp.value));
        return inp;
      };
      const motionSelect = (key, choices) => {
        const cur = L[key] && L[key].type ? L[key].type : "none";
        const sel = h("select", { class: "select sm" }, choices.map(c => h("option", { value: c, text: c })));
        sel.value = choices.includes(cur) ? cur : "none";
        sel.addEventListener("change", () => {
          if (sel.value === "none") set(key, null);
          else set(key, Object.assign({}, L[key] || {}, { type: sel.value, duration: (L[key] && L[key].duration) || (key === "enter" ? 0.3 : 0.25) }));
        });
        return sel;
      };
      const head = h("div", { class: "row-between", style: { marginBottom: "12px" } },
        h("h3", { class: "card-title" }, h("span", { class: "t-type ly-" + (L.type || "shape"), style: { width: "22px", height: "22px", borderRadius: "6px", display: "grid", placeItems: "center" } }, icon(LAYER_ICON[L.type] || "shape", 12)),
          "Layer " + (T.sel + 1) + " · " + (L.type || "?")),
        h("div", { class: "row", style: { gap: "6px" } },
          h("button", { class: "btn xs", type: "button", onclick: () => { T.spec.layers.splice(T.sel + 1, 0, clone(L)); T.sel += 1; markDirty(); paintPane(pane); } }, icon("copy", 13), "Duplicate"),
          h("button", { class: "btn xs danger", type: "button", onclick: () => { T.spec.layers.splice(T.sel, 1); T.sel = -1; markDirty(); paintPane(pane); } }, icon("trash", 13), "Delete")));
      const grid = h("div", { class: "inspector" });
      if (L.type === "text" || L.type === "counter") {
        const ta = h("textarea", { class: "textarea", rows: "2", value: L.text !== undefined ? String(L.text) : "" });
        ta.addEventListener("input", () => set("text", ta.value));
        if (L.type === "text") grid.appendChild(Field("Text", ta, { class: "wide" }));
        const color = h("input", { class: "input-color", type: "color", value: /^#[0-9a-f]{6}/i.test(L.color || "") ? L.color.slice(0, 7) : "#ffffff", "aria-label": "Colour" });
        const colorText = textInput("color", "#FFFFFF");
        color.addEventListener("input", () => { colorText.value = color.value.toUpperCase(); set("color", color.value.toUpperCase()); });
        add(grid, [Field("Font", textInput("font", "Montserrat Black"), { class: "half" }), Field("Size", numInput("size", "1")),
          Field("Colour", h("div", { class: "color-field" }, color, colorText))]);
        const caseSel = h("select", { class: "select sm" }, ["none", "upper", "lower", "title"].map(c => h("option", { value: c, text: c })));
        caseSel.value = L.case || "none";
        caseSel.addEventListener("change", () => set("case", caseSel.value));
        grid.appendChild(Field("Case", caseSel));
        if (L.type === "counter") add(grid, [Field("From", numInput("from", "1")), Field("To", numInput("to", "1")), Field("Prefix", textInput("prefix")), Field("Suffix", textInput("suffix"))]);
      }
      if (L.type === "video" || L.type === "image") {
        grid.appendChild(Field("Source", h("input", { class: "input sm", value: L.src || "", readonly: true, title: L.src || "" }), { class: "wide", hint: "Paths are relative to this timeline's folder; change sources in the JSON tab." }));
        if (L.type === "video") add(grid, [Field("In (s)", numInput("in")), Field("Speed", numInput("speed", "0.05")), Field("Volume", numInput("volume", "0.05"))]);
        const fit = h("select", { class: "select sm" }, ["cover", "contain", "fill"].map(c => h("option", { value: c, text: c })));
        fit.value = L.fit || "cover";
        fit.addEventListener("change", () => set("fit", fit.value));
        grid.appendChild(Field("Fit", fit));
      }
      if (L.type === "shape") {
        const color = textInput("color", "#FFE500");
        add(grid, [Field("Shape", textInput("shape", "rect")), Field("Width", numInput("w", "1")), Field("Height", numInput("h", "1")), Field("Colour", color)]);
      }
      add(grid, [Field("Start (s)", numInput("start")), Field("End (s)", numInput("end")), Field("X", posInput("x")), Field("Y", posInput("y")),
        Field("Opacity", numInput("opacity", "0.05", "1")), Field("Scale", numInput("scale", "0.05", "1")),
        Field("Enter", motionSelect("enter", ENTERS)), Field("Exit", motionSelect("exit", EXITS))]);
      clear(box).append(head, grid);
    }

    function paintCaptions(pane) {
      const cap = T.spec.captions;
      if (cap && Array.isArray(cap.words) && cap.words.length) {
        const words = h("div", { class: "chips", style: { maxHeight: "300px", overflowY: "auto" } });
        cap.words.forEach((w, i) => {
          const inp = h("input", { class: "input sm", value: w.w, title: num(w.s, 0).toFixed(2) + "s", "aria-label": "Caption word " + (i + 1),
                                   style: { width: Math.max(4, String(w.w).length + 2) + "ch", height: "28px", padding: "2px 6px" } });
          inp.addEventListener("change", () => { w.w = inp.value; markDirty(); });
          inp.addEventListener("focus", () => setPlayhead(num(w.s, 0), true));
          words.appendChild(inp);
        });
        pane.appendChild(h("div", { class: "stack" },
          h("div", { class: "row-between" }, h("h3", { class: "card-title" }, icon("cc", 16), "Caption words (" + cap.words.length + ")"),
            h("span", { class: "muted small", text: (cap.font ? cap.font + " · " : "") + (cap.style_dna ? "style from " + basename(cap.style_dna) : cap.style ? "custom style" : "default style") })),
          h("p", { class: "field-hint", text: "Fix a misheard word here; timing stays the same. Focus a word to jump the playhead to it." }),
          words));
      } else pane.appendChild(h("p", { class: "muted", text: "No burned-in captions in this timeline." }));
      const audio = T.spec.audio || [];
      pane.appendChild(h("hr", { class: "divider" }));
      pane.appendChild(h("h3", { class: "card-title", style: { marginBottom: "10px" } }, icon("wave", 16), "Audio (" + audio.length + ")"));
      if (!audio.length) { pane.appendChild(h("p", { class: "muted small", text: "No separate audio; clip sound comes from video layers." })); return; }
      pane.appendChild(h("table", { class: "table" },
        h("thead", null, h("tr", null, ["Role", "Source", "Start", "Volume"].map(t => h("th", { text: t })))),
        h("tbody", null, audio.map(a => {
          const vol = h("input", { class: "input sm", type: "number", step: "0.05", min: "0", max: "4", value: a.volume !== undefined ? a.volume : 1, style: { width: "86px" } });
          vol.addEventListener("change", () => { a.volume = parseFloat(vol.value); markDirty(); });
          const st = h("input", { class: "input sm", type: "number", step: "0.05", min: "0", value: num(a.start, 0), style: { width: "86px" } });
          st.addEventListener("change", () => { a.start = parseFloat(st.value); markDirty(); });
          return h("tr", null, h("td", null, h("span", { class: "pill plain", text: a.role || (a.sfx ? "sfx" : "audio") })),
            h("td", { class: "ellipsis", style: { maxWidth: "260px" }, text: a.sfx ? "sfx: " + a.sfx : basename(a.src) }), h("td", null, st), h("td", null, vol));
        }))));
    }

    function paintJson(pane) {
      const ta = h("textarea", { class: "textarea code", spellcheck: "false", value: JSON.stringify(T.spec, null, 2), "aria-label": "Timeline JSON" });
      const msg = h("div", { class: "small muted", text: "Edit, then Apply. Saving validates the whole timeline." });
      pane.append(ta, h("div", { class: "row-between", style: { marginTop: "10px" } }, msg,
        h("div", { class: "row" },
          h("button", { class: "btn sm", type: "button", onclick: () => { try { ta.value = JSON.stringify(JSON.parse(ta.value), null, 2); } catch (e) { msg.textContent = "Not valid JSON: " + e.message; msg.className = "small file-meta err"; } } }, "Reformat"),
          h("button", { class: "btn sm primary", type: "button", onclick: () => {
            try {
              const spec = JSON.parse(ta.value);
              if (!spec || typeof spec !== "object" || Array.isArray(spec)) throw new Error("the timeline must be a JSON object");
              T.spec = spec; T.sel = -1; markDirty();
              msg.textContent = "Applied. Save to keep it."; msg.className = "small muted";
            } catch (e) { msg.textContent = "Not valid JSON: " + e.message; msg.className = "small file-meta err"; }
          } }, "Apply"))));
    }

    function askCard() {
      const logBox = h("div", { class: "chat-log", "aria-live": "polite" });
      const family = T.path.replace(/(\.v\d+)?\.json$/, "");
      const past = ((S.project && S.project.asks) || []).filter(a => (a.spec_path || "").replace(/(\.v\d+)?\.json$/, "") === family).slice(-8);
      past.forEach(a => {
        logBox.appendChild(h("div", { class: "bubble me", text: a.message }));
        logBox.appendChild(h("div", { class: "bubble " + (a.ok ? "ai" : "err") }, a.summary || (a.ok ? "Done." : "That change did not validate."),
          a.new_path ? h("span", { class: "b-meta", text: "saved " + basename(a.new_path) }) : null));
      });
      if (!past.length) logBox.appendChild(h("p", { class: "muted small", style: { margin: 0 }, text: "Try: “make the hook title yellow and bigger”, “add a stat at 4 s: 73% quit in week one”, “end 2 seconds earlier”." }));
      const input = h("textarea", { class: "textarea", rows: "2", maxlength: "4000", placeholder: "Ask Claude to change this timeline...", "aria-label": "Ask Claude to change this timeline" });
      const send = h("button", { class: "icon-btn", type: "button", title: "Send", "aria-label": "Send to Claude" }, icon("send", 17));
      const ask = async () => {
        const message = input.value.trim();
        if (!message) return;
        if (T.dirty) { const p = await save(false); if (!p) return; }
        const empty = logBox.querySelector("p");
        if (empty) empty.remove();
        logBox.appendChild(h("div", { class: "bubble me", text: message }));
        const typing = h("div", { class: "typing", "aria-label": "Claude is working" }, h("i"), h("i"), h("i"), h("span", { class: "small muted", style: { marginLeft: "6px" }, text: "0s" }));
        logBox.appendChild(typing);
        logBox.scrollTop = logBox.scrollHeight;
        const t0 = Date.now();
        const timer = setInterval(() => { typing.lastChild.textContent = Math.round((Date.now() - t0) / 1000) + "s"; }, 1000);
        input.value = ""; input.disabled = true; send.disabled = true;
        try {
          const r = await api("POST", "/api/projects/" + encodeURIComponent(S.pid) + "/ask", { message, spec_path: T.path });
          typing.remove();
          logBox.appendChild(h("div", { class: "bubble ai" }, r.summary || "Done.", h("span", { class: "b-meta", text: "saved " + basename(r.path) })));
          toast("Claude saved " + basename(r.path), { kind: "ok", actions: [{ label: "Render preview", run: () => renderSpec({ preview: true }) }] });
          await loadProject();
          await open(r.path);
        } catch (e) {
          typing.remove();
          logBox.appendChild(h("div", { class: "bubble err" }, e.message, e.data && e.data.errors ? h("ul", { class: "errors" }, e.data.errors.slice(0, 8).map(x => h("li", { text: x }))) : null));
          input.value = message;
        } finally {
          clearInterval(timer);
          input.disabled = false; send.disabled = false;
          logBox.scrollTop = logBox.scrollHeight;
        }
      };
      send.addEventListener("click", ask);
      input.addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask(); } });
      return h("section", { class: "card chat" },
        h("div", { class: "row-between" }, h("h2", { class: "card-title" }, icon("wand", 16), "Ask Claude to change it"),
          h("span", { class: "muted small", text: "saves a new version" })),
        logBox, h("div", { class: "chat-input" }, input, send));
    }

    const watcher = { update: () => {
      if (!T.data) return;
      const done = S.jobs.find(j => j.kind === "render" && j.status === "done" && j.args && j.args.spec === T.path && j.id === renderJobId);
      if (done && !T.dirty) { renderJobId = null; api("GET", "/api/projects/" + encodeURIComponent(S.pid) + "/spec?path=" + encodeURIComponent(T.path)).then(d => { T.data.renders = d.renders; }).catch(() => {}); }
    } };

    if (T.path && S.pid) open(T.path);
    else {
      const first = specs().find(s => s.kind === "spec");
      if (first && S.pid) open(first.path); else paint();
    }
    S.leaveGuard = () => T.dirty;
    window.onbeforeunload = e => { if (T.dirty) { e.preventDefault(); e.returnValue = ""; } };
    return { el, panels: [watcher] };
  }

  // ------------------------------------------------------------------ views: templates

  async function templatesProject() {
    if (S.tplProject) return S.tplProject;
    const found = S.projects.find(p => p.name === "Template previews");
    if (found) { S.tplProject = found.id; return found.id; }
    const p = await api("POST", "/api/projects", { name: "Template previews" });
    S.tplProject = p.id;
    await refreshProjects();
    return p.id;
  }

  function viewTemplates() {
    const el = h("div", { class: "fade-in" });
    const grid = h("div", { class: "tpl-grid" });
    const search = h("input", { class: "input search", type: "search", placeholder: "Search templates...", "aria-label": "Search templates" });
    const aspect = h("select", { class: "select sm", "aria-label": "Aspect ratio" }, ASPECT_OPTS.map(o => h("option", { value: o.value, text: o.label })));
    aspect.value = store.get("tpl.aspect", "9:16");
    const energy = h("select", { class: "select sm", "aria-label": "Energy" }, ["high", "medium", "calm"].map(e => h("option", { value: e, text: e + " energy" })));
    energy.value = store.get("tpl.energy", "high");
    const cats = h("div", { class: "chips" });
    let cat = "all";
    const previews = store.get("tpl.previews", {});
    const busy = {};

    const key = (name, still) => [name, aspect.value, energy.value, still ? "png" : "mp4"].join("|");

    async function preview(name, still, card) {
      const k = key(name, still);
      try {
        const pid = await templatesProject();
        const job = await api("POST", "/api/projects/" + encodeURIComponent(pid) + "/jobs",
          { kind: "template", args: { name, aspect: aspect.value, energy: energy.value, still } });
        busy[k] = true;
        paintCard(card, name);
        watchJob(job.id, (j, err) => {
          if (err || !j || !ACTIVE(j.status)) {
            delete busy[k];
            if (j && j.status === "done" && j.primary) { previews[k] = { pid, path: j.primary, t: Date.now() }; store.set("tpl.previews", previews); }
            else if (j && j.status === "failed") toast("Preview failed: " + name, { kind: "bad", message: j.error || "", actions: [{ label: "See log", run: () => go("jobs", { job: j.id }) }] });
            paintCard(card, name);
          }
        });
      } catch (e) { fail(e, "Could not start the preview"); }
    }

    function paintCard(card, name) {
      const t = S.templates[name];
      const pv = card.querySelector(".tpl-preview");
      clear(pv);
      pv.className = "tpl-preview" + (aspect.value === "16:9" ? " landscape" : "");
      const clip = previews[key(name, false)], still = previews[key(name, true)];
      if (clip) {
        const v = h("video", { src: fileUrl(clip.pid, clip.path), muted: true, loop: true, playsinline: true, autoplay: true, preload: "metadata" });
        v.muted = true;
        v.addEventListener("error", () => { delete previews[key(name, false)]; store.set("tpl.previews", previews); paintCard(card, name); });
        pv.appendChild(v);
      } else if (still) {
        const img = h("img", { src: fileUrl(still.pid, still.path), alt: name + " preview", loading: "lazy" });
        img.addEventListener("error", () => { delete previews[key(name, true)]; store.set("tpl.previews", previews); paintCard(card, name); });
        pv.appendChild(img);
      } else {
        pv.appendChild(h("div", { class: "tpl-ph" }, icon("grid", 26), h("b", { text: name.replace(/_/g, " ") }), h("span", { text: t.category })));
      }
      if (busy[key(name, true)] || busy[key(name, false)]) pv.appendChild(h("div", { class: "tpl-busy" }, h("span", { class: "spinner" })));
    }

    function paintGrid() {
      clear(grid);
      const q = search.value.trim().toLowerCase();
      const names = Object.keys(S.templates).filter(n => (cat === "all" || S.templates[n].category === cat) &&
        (!q || n.includes(q) || (S.templates[n].description || "").toLowerCase().includes(q)));
      if (!names.length) { grid.appendChild(h("p", { class: "muted", text: "No template matches." })); return; }
      for (const name of names) {
        const t = S.templates[name];
        const params = Object.entries(t.params || {});
        const card = h("article", { class: "tpl" },
          h("div", { class: "tpl-preview" }),
          h("div", { class: "tpl-body" },
            h("div", { class: "row-between" }, h("span", { class: "tpl-name", text: name }), h("span", { class: "pill kind plain", text: t.category })),
            h("div", { class: "tpl-desc", title: t.description, text: t.description }),
            params.length ? h("details", null, h("summary", { text: params.length + " parameter" + (params.length === 1 ? "" : "s") }),
              h("dl", null, params.map(([p, spec]) => [h("dt", { text: p + " (" + spec.type + ")" }),
                h("dd", { text: (spec.doc || "") + (spec.default !== undefined && spec.default !== "" ? " Default: " + JSON.stringify(spec.default) : "") })]))) : null,
            h("div", { class: "tpl-actions" },
              h("button", { class: "btn xs", type: "button", onclick: () => preview(name, true, card) }, icon("image", 13), "Still"),
              h("button", { class: "btn xs", type: "button", onclick: () => preview(name, false, card) }, icon("play", 12), "Clip"))));
        paintCard(card, name);
        grid.appendChild(card);
      }
    }

    async function load() {
      clear(grid).appendChild(h("div", { class: "row muted" }, h("span", { class: "spinner dark" }), "Loading the catalog..."));
      try {
        if (!S.templates) S.templates = await api("GET", "/api/templates");
      } catch (e) {
        clear(grid).appendChild(h("div", { class: "callout bad" }, icon("xcircle"), h("div", { text: e.message })));
        return;
      }
      const categories = Array.from(new Set(Object.values(S.templates).map(t => t.category)));
      clear(cats);
      ["all"].concat(categories).forEach(c => cats.appendChild(h("button", { class: "chip chip-filter", type: "button", "aria-pressed": c === cat ? "true" : "false",
        onclick: e => { cat = c; cats.querySelectorAll("button").forEach(b => b.setAttribute("aria-pressed", "false")); e.currentTarget.setAttribute("aria-pressed", "true"); paintGrid(); } },
        c === "all" ? "All (" + Object.keys(S.templates).length + ")" : c)));
      paintGrid();
    }

    search.addEventListener("input", paintGrid);
    aspect.addEventListener("change", () => { store.set("tpl.aspect", aspect.value); paintGrid(); });
    energy.addEventListener("change", () => { store.set("tpl.energy", energy.value); paintGrid(); });
    el.append(h("div", { class: "tpl-toolbar" }, search, aspect, energy), h("div", { style: { marginBottom: "16px" } }, cats), grid);
    load();
    const allStills = h("button", { class: "btn sm", type: "button", onclick: async () => {
      const names = Array.from(grid.querySelectorAll(".tpl")).map(c => c.querySelector(".tpl-name").textContent).filter(n => !previews[key(n, true)]);
      if (!names.length) { toast("Every visible template has a still", { kind: "info" }); return; }
      if (!confirm("Render " + names.length + " still previews? They queue one after another.")) return;
      for (const n of names) await preview(n, true, Array.from(grid.querySelectorAll(".tpl")).find(c => c.querySelector(".tpl-name").textContent === n));
    } }, icon("image", 14), "Render missing stills");
    return { el, actions: [allStills] };
  }

  // ------------------------------------------------------------------ views: settings

  function viewSettings() {
    const el = h("div", { class: "stack fade-in" });
    const checks = h("div");
    const summary = h("div");
    async function load(refresh) {
      clear(checks).appendChild(h("div", { class: "row muted" }, h("span", { class: "spinner dark" }), "Checking this computer..."));
      try {
        S.doctor = await api("GET", "/api/doctor" + (refresh ? "?refresh=1" : ""));
      } catch (e) {
        clear(checks).appendChild(h("div", { class: "callout bad" }, icon("xcircle"), h("div", { text: e.message })));
        return;
      }
      paintHealth();
      const bad = S.doctor.filter(c => !c.ok && c.required), warn = S.doctor.filter(c => !c.ok && !c.required);
      clear(summary).appendChild(bad.length ? h("div", { class: "callout bad" }, icon("xcircle"), h("div", null, h("b", { text: "Not ready. " }), "Fix the red lines below; nothing renders without them."))
        : warn.length ? h("div", { class: "callout warn" }, icon("warn"), h("div", null, h("b", { text: "Ready. " }), warn.length + " optional feature" + (warn.length === 1 ? " is" : "s are") + " off; each line says how to turn it on."))
        : h("div", { class: "callout ok" }, icon("check"), h("div", null, h("b", { text: "Everything is ready." }), " Renders, analysis, captions and Claude planning all work.")));
      clear(checks).appendChild(h("table", { class: "table checks-table" }, h("tbody", null, S.doctor.map(c => {
        const state = c.ok ? "ok" : c.required ? "bad" : "warn";
        return h("tr", null,
          h("td", { class: "status" }, h("span", { class: "status-icon " + state, title: state }, icon(c.ok ? "check" : c.required ? "xcircle" : "warn", 15))),
          h("td", null, h("div", { style: { fontWeight: 600 }, text: c.name }), h("div", { class: "muted small", text: c.detail }),
            c.fix ? h("div", { class: "fix" }, h("code", { text: c.fix }),
              h("button", { class: "icon-btn sm", type: "button", title: "Copy", "aria-label": "Copy fix for " + c.name,
                            onclick: async () => { try { await navigator.clipboard.writeText(c.fix); toast("Copied", { kind: "ok" }); } catch (e) { fail(e, "Could not copy"); } } }, icon("copy", 14))) : null),
          h("td", { class: "num" }, h("span", { class: "pill plain " + (c.ok ? "done" : c.required ? "failed" : "cancelled"), text: c.ok ? "ok" : c.required ? "required" : "optional" })));
      }))));
    }
    const fontsCmd = 'python -m vstudio fonts install "Montserrat" "Anton" "Bebas Neue" "Poppins" "Archivo Black" "Bangers"';
    const health = S.health || {};
    el.append(
      h("section", { class: "card" }, h("div", { class: "card-head" }, h("h2", { class: "card-title" }, icon("pulse", 16), "Engine health"),
        h("button", { class: "btn sm", type: "button", onclick: () => load(true) }, icon("refresh", 14), "Check again")), summary, h("div", { style: { marginTop: "14px" } }, checks)),
      h("div", { class: "grid-2" },
        h("section", { class: "card" }, h("h2", { class: "card-title", style: { marginBottom: "10px" } }, icon("font", 16), "Caption fonts"),
          h("p", { class: "muted small", style: { marginTop: 0 }, text: "Viral caption looks need heavy display fonts. These are free Google Fonts (OFL), installed into workspace/fonts for the studio only." }),
          h("div", { class: "fix" }, h("code", { text: fontsCmd }), h("button", { class: "icon-btn sm", type: "button", "aria-label": "Copy font install command",
            onclick: async () => { try { await navigator.clipboard.writeText(fontsCmd); toast("Copied", { kind: "ok" }); } catch (e) { fail(e, "Could not copy"); } } }, icon("copy", 14))),
          h("p", { class: "small", style: { marginBottom: 0 } }, "Or download any family from ", h("a", { href: "https://fonts.google.com", target: "_blank", rel: "noopener noreferrer", text: "fonts.google.com" }),
            " and drop the .ttf files into workspace/fonts.")),
        h("section", { class: "card" }, h("h2", { class: "card-title", style: { marginBottom: "10px" } }, icon("wand", 16), "Claude"),
          h("p", { class: "muted small", style: { marginTop: 0 }, text: "Planning, scripts and “Ask Claude” run through Claude Code on this computer with your own subscription. No API key, no cloud render service." }),
          h("div", { class: "row" }, h("span", { class: "pill " + (health.claude ? "done" : "cancelled"), text: health.claude ? "Claude Code found" : "Claude Code not found" })),
          !health.claude ? h("div", { class: "fix" }, h("code", { text: "npm install -g @anthropic-ai/claude-code   then run: claude" })) : null)),
      h("div", { class: "grid-2" },
        h("section", { class: "card" }, h("h2", { class: "card-title", style: { marginBottom: "12px" } }, icon("folder", 16), "Where things are"),
          h("dl", { class: "kv" },
            h("dt", { text: "Projects" }), h("dd", { class: "mono", text: (health.workspace || "") + "/projects" }),
            h("dt", { text: "Engine" }), h("dd", { class: "mono", text: health.repo || "" }),
            h("dt", { text: "Address" }), h("dd", { class: "mono", text: location.host + " (this computer only)" }),
            h("dt", { text: "Upload limit" }), h("dd", { text: fmtBytes(health.max_upload) }),
            h("dt", { text: "Version" }), h("dd", { text: health.version || "" }))),
        h("section", { class: "card" }, h("h2", { class: "card-title", style: { marginBottom: "10px" } }, icon("film", 16), "2D animated remakes"),
          h("p", { class: "muted small", style: { marginTop: 0 }, text: "ReelMimic rebuilds a reference reel as a 2D animation (painted, pixel, paper cut-out, anime cel). It runs as its own local app." }),
          h("div", { class: "fix" }, h("code", { text: "python -m vstudio mimic2d start" })),
          h("p", { class: "small", style: { marginBottom: 0 } }, "Then open ", h("a", { href: "http://localhost:4318", target: "_blank", rel: "noopener noreferrer", text: "localhost:4318" }), "."))));
    load(false);
    return el;
  }

  // ------------------------------------------------------------------ chrome: nav, health, boot

  function buildNav() {
    const nav = clear($("#nav"));
    NAV.forEach(group => {
      const g = h("div", { class: "nav-group" });
      if (group.label) g.appendChild(h("div", { class: "eyebrow nav-eyebrow", text: group.label }));
      group.items.forEach(name => {
        const v = VIEWS[name];
        g.appendChild(h("a", { class: "nav-item", href: "#/" + name, dataset: { view: name }, title: v.title },
          icon(v.icon, 18), h("span", { class: "nav-label", text: v.title }),
          name === "jobs" ? h("span", { class: "nav-badge", id: "jobs-badge", hidden: true, text: "0" }) : null));
      });
      nav.appendChild(g);
    });
  }

  function paintHealth() {
    const foot = $("#health-foot");
    const dot = foot.querySelector(".dot");
    const text = foot.querySelector(".health-text");
    if (!S.health) { dot.className = "dot dot-bad"; text.textContent = "Studio not answering"; return; }
    if (!S.doctor) { dot.className = "dot dot-idle"; text.textContent = "Checking engine..."; return; }
    const bad = S.doctor.filter(c => !c.ok && c.required).length;
    const warn = S.doctor.filter(c => !c.ok && !c.required).length;
    dot.className = "dot " + (bad ? "dot-bad" : warn ? "dot-warn" : "dot-ok");
    text.textContent = bad ? "Setup needed" : warn ? "Ready · " + warn + " optional off" : "Engine ready";
    $("#foot-note").textContent = location.host + " · local only";
  }

  async function heartbeat() {
    try { S.health = await api("GET", "/api/health"); }
    catch (e) { S.health = null; }
    paintHealth();
    setTimeout(heartbeat, 15000);
  }

  async function boot() {
    buildNav();
    $("#new-project-btn").appendChild(icon("plus", 18));
    $("#new-project-btn").addEventListener("click", newProjectDialog);
    $("#project-select").addEventListener("change", e => {
      if (e.target.value && e.target.value !== S.pid) selectProject(e.target.value);
    });
    window.addEventListener("hashchange", onHashChange);
    document.addEventListener("keydown", e => {
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter" && S.primary) { e.preventDefault(); S.primary(); }
    });
    document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") poll(); });
    try { S.health = await api("GET", "/api/health"); } catch (e) { S.health = null; }
    paintHealth();
    try {
      await refreshProjects();
      const saved = store.get("project", null);
      const visible = S.projects.filter(p => p.name !== "Template previews");
      S.pid = (saved && S.projects.some(p => p.id === saved)) ? saved : (visible[0] ? visible[0].id : null);
      await loadProject();
      await refreshProjects();
    } catch (e) { fail(e, "Could not load projects"); }
    updateBadge();
    render();
    poll();
    setTimeout(heartbeat, 15000);
    api("GET", "/api/doctor").then(d => { S.doctor = d; paintHealth(); }).catch(() => {});
  }

  boot();
})();
