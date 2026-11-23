// 作战面板：今日盈亏（吉凶字，原来是像素图标）+ 纪律警报（大感叹号）+ 打法 C 持仓状态。盯盘界面和控制台共用。只读。
(function () {
  // 12×12 像素图：. 透明，其余字母对应颜色
  const ICONS = {
    win: { pal: { g: "#39ff88", d: "#0a7a3c", y: "#ffd400", w: "#eafff3" }, map: [
      "....yyyy....", "..yywwyyyy..", ".yywyyyyyyy.", ".ywyyddyyyy.", "yywyyddyyyyy", "yyyyddddyyyy",
      "yyyyyddyyyyy", "yyyyyddyyyyy", ".yyyyddyyyy.", ".yyyyyyyyyy.", "..yyyyyyyy..", "....yyyy...." ] },
    rocket: { pal: { g: "#39ff88", w: "#eafff3", c: "#00e5ff", o: "#ffb000", r: "#ff2e63" }, map: [
      ".....gg.....", "....gwwg....", "....gwwg....", "...ggccgg...", "...gwccwg...", "...gwwwwg...",
      "..ggwwwwgg..", ".gg.gwwg.gg.", ".g..gggg..g.", ".....oo.....", "....orro....", ".....rr....." ] },
    skull: { pal: { w: "#ffd6e0", r: "#ff2e63", k: "#06050d" }, map: [
      "...wwwwww...", "..wwwwwwww..", ".wwwwwwwwww.", ".wwkkwwkkww.", ".wwkkwwkkww.", ".wwwwkkwwww.",
      "..wwwwwwww..", "...wkwkwk...", "...wwwwww...", ".r.r.r.r.r..", "r.r.r.r.r.r.", "............" ] },
    flat: { pal: { c: "#00e5ff", k: "#06050d" }, map: [
      "..cccccccc..", ".cccccccccc.", "cccccccccccc", "cckkcccckkcc", "cckkcccckkcc", "cccccccccccc",
      "cccccccccccc", "cckkkkkkkkcc", "cccccccccccc", ".cccccccccc.", "..cccccccc..", "............" ] },
  };
  function icon(name, px) {
    const ic = ICONS[name], cv = document.createElement("canvas");
    cv.width = 12 * px; cv.height = 12 * px;
    const g = cv.getContext("2d");
    ic.map.forEach((row, y) => [...row].forEach((ch, x) => { if (ch !== ".") { g.fillStyle = ic.pal[ch]; g.fillRect(x * px, y * px, px, px); } }));
    return cv;
  }
  const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const money = v => v == null ? "—" : (v >= 0 ? "+" : "-") + "$" + Math.abs(v).toLocaleString("en-US", { maximumFractionDigits: 0 });
  let audio = null, lastStop = "";
  document.addEventListener("click", () => { try { audio = audio || new (window.AudioContext || window.webkitAudioContext)(); } catch (e) {} }, { once: true });
  function beep() {
    if (!audio) return;
    [0, .18, .36].forEach(t => { const o = audio.createOscillator(), v = audio.createGain(); o.type = "square"; o.frequency.value = 880;
      v.gain.value = .08; o.connect(v); v.connect(audio.destination); o.start(audio.currentTime + t); o.stop(audio.currentTime + t + .12); });
  }
  function rbar(r) {
    const lo = -1.2, hi = 3.2, P = v => Math.max(0, Math.min(100, (v - lo) / (hi - lo) * 100));
    const col = r.R >= 1 ? "var(--up)" : r.R >= 0 ? "var(--accent)" : "var(--down)";
    const a = P(Math.min(0, r.R)), b = P(Math.max(0, r.R));
    return `<div class="cy-rbar"><i style="left:${a}%;width:${b - a}%;background:${col};opacity:.75"></i>
      <i class="s" style="left:${P(-1)}%"></i><i class="z" style="left:${P(0)}%"></i><i class="g" style="left:${P(1)}%"></i><i class="g" style="left:${P(2)}%"></i></div>
      <div class="cy-rlab"><span>止损 -1R</span><span>0</span><span>走出来 +1R</span><span>锁利 +2R</span></div>`;
  }
  function card(r) {
    const nm = r.kind === "期权" ? `${r.und} ${r.strike}${r.opt === "CALL" ? "C" : "P"} ${String(r.expiry || "").slice(5)}` : r.und;
    return `<div class="cy-card ${r.lvl}">
      <div class="hd"><span class="nm">${esc(nm)}</span><span class="small">${r.dir} ×${r.qty}${r.since ? " · " + r.since + " 起" : ""}${r.held != null ? " · " + r.held + " 分钟" : ""}</span><span class="st">${esc(r.state)}</span></div>
      <div class="nums"><span>成本 ${r.cost}</span><span>现价 ${r.now}</span><span class="down">止损 ${r.stop}</span><span class="${r.R >= 0 ? "up" : "down"}">${r.R >= 0 ? "+" : ""}${r.R}R</span><span class="${r.pl >= 0 ? "up" : "down"}">${money(r.pl)}</span></div>
      ${rbar(r)}
      <div class="act">${esc(r.act)}</div>
      ${r.und_vwap ? `<div class="small">正股 ${r.und_px != null ? r.und_px.toFixed(2) : "—"} · VWAP ${r.und_vwap} · ${r.vwap_ok > 0 ? "在有利一侧" : r.vwap_ok < 0 ? "在不利一侧" : "—"}</div>` : ""}
      ${(r.flags || []).map(f => `<span class="flag">！${esc(f)}</span>`).join("")}
    </div>`;
  }
  async function load(root, api) {
    let d;
    try { const res = await fetch(api, { cache: "no-store" }); d = await res.json(); } catch (e) { d = { ok: false, err: "连不上：" + e.message }; }
    const hero = root.querySelector(".cy-hero"), right = root.querySelector(".cy-right");
    if (!d.ok) {
      hero.className = "cy-box cy-hero cy-flat";
      hero.innerHTML = `<div class="cy-t">TODAY P&L</div><div class="cy-sub">读不到持仓：${esc(d.err || "OpenD 没连上")}</div>`;
      right.innerHTML = ""; document.body.classList.remove("cy-red"); return;
    }
    const t = d.total || 0, kind = t > 0.5 ? "win" : t < -0.5 ? "lose" : "flat";
    hero.className = "cy-box cy-hero cy-" + kind;
    hero.innerHTML = `<div class="cy-t">TODAY P&L · ${esc(d.at)} 美东</div>
      <div class="cy-pnl"><span class="ic"></span><div><div class="cy-big">${money(t)}</div>
      <div class="cy-sub">持仓 ${money(d.open_pl)} · 已平 ${money(d.closed_pl)} · ${d.n_trades} 笔${d.streak ? ` · 连亏 ${d.streak}` : ""}${d.limit ? ` · 上限 -$${d.limit}` : ""}</div></div></div>
      <div class="cy-rules"><span>止损只往有利方向挪</span><span>到止损就走</span><span>连亏 ${d.rules["连亏停手笔数"]} 笔停手</span><span>试仓 ${d.rules["试仓时限_分钟"]} 分钟不走出来就砍</span><span>只做名单内</span></div>
      ${d.closed && d.closed.length ? `<div class="cy-closed">已平：${d.closed.map(c => `${c.t} ${c.code.replace(/\d{6}([CP])\d+/, "$1")} <span class="${c.pl >= 0 ? "up" : "down"}">${money(c.pl)}</span>`).join(" · ")}</div>` : ""}`;
    // 玉衡 5.0：像素表情换成和玉衡标志同一路的草书字——赚超 $300 大吉，赚 小吉，亏 小凶，亏超 $300 大凶，没盈亏 平
    const luck = t > 300 ? ["大吉", "big good"] : t > 0.5 ? ["小吉", "good"] : t < -300 ? ["大凶", "big bad"] : t < -0.5 ? ["小凶", "bad"] : ["平", "flat"];
    hero.querySelector(".ic").innerHTML = `<span class="yh-luck ${luck[1]}" title="今日盈亏 ${money(t)}">${luck[0]}</span>`;
    const stops = d.alerts.filter(a => a.lvl === "stop"), warns = d.alerts.filter(a => a.lvl === "warn");
    let h = "";
    if (stops.length) h += `<div class="cy-alarm"><div class="cy-bang">!</div><ul>${stops.map(a => `<li>${esc(a.text)}</li>`).join("")}</ul></div>`;
    if (warns.length) h += `<div class="cy-alarm warn"><div class="cy-bang">!</div><ul>${warns.map(a => `<li>${esc(a.text)}</li>`).join("")}</ul></div>`;
    h += `<div class="cy-box"><div class="cy-t">POSITIONS · 打法 C 滚仓监测</div>${d.positions.length ? `<div class="cy-pos">${d.positions.map(card).join("")}</div>` : `<div class="cy-none">现在没有持仓。下单前先说：做什么、为什么、在不在名单里。</div>`}</div>`;
    right.innerHTML = h;
    document.body.classList.toggle("cy-red", stops.length > 0);
    const key = stops.map(a => a.text).join("|");
    if (key && key !== lastStop) beep();
    lastStop = key;
  }
  window.cyMount = function (el, api, every) {
    el.classList.add("cy-panel");
    el.innerHTML = `<div class="cy-box cy-hero cy-flat"><div class="cy-t">TODAY P&L</div><div class="cy-sub">载入中…</div></div><div class="cy-right"></div>`;
    load(el, api); setInterval(() => load(el, api), (every || 5) * 1000);
  };
})();
