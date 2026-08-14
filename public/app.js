const SUITS = ["S", "D", "H", "C"];
const SUIT_SYMBOL = { S: "♠", D: "♦", H: "♥", C: "♣" };
const SUIT_NAME = { S: "Spades", D: "Diamonds", H: "Hearts", C: "Clubs" };
const RED_SUITS = new Set(["D", "H"]);

let ws = null;
let myId = null;
let myCode = null;
let state = null; // last full state from server
let selectedNumPlayers = 5;
let partnerBuild = { suit: null, rank: null, occurrence: null }; // in-progress partner pick

const $ = (id) => document.getElementById(id);

function showScreen(name) {
  document.querySelectorAll(".screen").forEach((s) => s.classList.remove("active"));
  $(`screen-${name}`).classList.add("active");
}

function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.style.display = "block";
  clearTimeout(toast._h);
  toast._h = setTimeout(() => (t.style.display = "none"), 2600);
}

// ---------------- connection ----------------
// A heartbeat is required because many mobile/carrier NATs silently kill an
// idle TCP connection after 30-60s -- the browser's WebSocket object is left
// as a zombie that still reports readyState OPEN and never fires onclose,
// so without an app-level ping/pong there is no way to detect the dead
// connection short of the user manually refreshing.
let connecting = false;
let heartbeatInterval = null;
let heartbeatTimeout = null;
let reconnectTimer = null;

function connect() {
  if (connecting) return;
  connecting = true;
  if (new URLSearchParams(location.search).has("reset")) {
    localStorage.removeItem("kaadi3_session");
  }
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => {
    connecting = false;
    startHeartbeat();
    const saved = JSON.parse(localStorage.getItem("kaadi3_session") || "null");
    if (saved && saved.code && saved.playerId) {
      send({ type: "rejoin", code: saved.code, playerId: saved.playerId });
    }
  };
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "pong") { clearTimeout(heartbeatTimeout); return; }
    handleMessage(msg);
  };
  ws.onclose = () => {
    connecting = false;
    stopHeartbeat();
    scheduleReconnect();
  };
  ws.onerror = () => {
    try { ws.close(); } catch (e) {}
  };
}

function scheduleReconnect(delay = 1200) {
  clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(connect, delay);
}

function startHeartbeat() {
  stopHeartbeat();
  heartbeatInterval = setInterval(() => {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    send({ type: "ping" });
    clearTimeout(heartbeatTimeout);
    heartbeatTimeout = setTimeout(() => {
      // no pong came back in time -- treat the connection as dead.
      // Kept generous: the host runs on a very CPU-limited free tier, and a
      // tight timeout here causes false-positive "disconnects" under load
      // that just add more reconnect churn.
      try { ws.close(); } catch (e) {}
    }, 18000);
  }, 20000);
}

function stopHeartbeat() {
  clearInterval(heartbeatInterval);
  clearTimeout(heartbeatTimeout);
}

// Mobile browsers frequently suspend background tabs (app switch, screen
// lock). When the player comes back, verify the socket actually still
// works rather than waiting for a normal reconnect cycle to notice.
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible") return;
  if (!ws || ws.readyState === WebSocket.CLOSED || ws.readyState === WebSocket.CLOSING) {
    clearTimeout(reconnectTimer);
    connect();
  } else if (ws.readyState === WebSocket.OPEN) {
    send({ type: "ping" });
    clearTimeout(heartbeatTimeout);
    heartbeatTimeout = setTimeout(() => { try { ws.close(); } catch (e) {} }, 12000);
  }
});

function send(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
}

function handleMessage(msg) {
  if (msg.type === "joined") {
    myId = msg.playerId;
    myCode = msg.code;
    localStorage.setItem("kaadi3_session", JSON.stringify({ code: myCode, playerId: myId }));
    $("home-error").textContent = "";
  } else if (msg.type === "session_gone") {
    // The room no longer exists server-side (e.g. the host process
    // restarted). Don't leave the player frozen on a stale screen --
    // drop back to home so they can start or join a fresh game.
    localStorage.removeItem("kaadi3_session");
    myId = null;
    state = null;
    render();
    toast("Your game session ended (server restarted) — start or join a new game.");
  } else if (msg.type === "name_taken") {
    $("home-error").textContent = "That name is taken in this room — pick another.";
  } else if (msg.type === "error") {
    toast(msg.message);
  } else if (msg.type === "state") {
    state = msg;
    render();
  }
}

// ---------------- home screen ----------------
$("num-players-picker").addEventListener("click", (e) => {
  const btn = e.target.closest(".chip");
  if (!btn) return;
  selectedNumPlayers = parseInt(btn.dataset.n, 10);
  document.querySelectorAll("#num-players-picker .chip").forEach((c) => c.classList.remove("selected"));
  btn.classList.add("selected");
});
document.querySelector('#num-players-picker .chip[data-n="5"]').classList.add("selected");

$("btn-create").addEventListener("click", () => {
  const name = $("name-input").value.trim();
  if (!name) { $("home-error").textContent = "Enter a name first."; return; }
  send({ type: "create_room", name, numPlayers: selectedNumPlayers });
});

$("btn-join").addEventListener("click", () => {
  const name = $("name-input").value.trim();
  const code = $("join-code-input").value.trim().toUpperCase();
  if (!name) { $("home-error").textContent = "Enter a name first."; return; }
  if (!code) { $("home-error").textContent = "Enter a room code."; return; }
  send({ type: "join_room", name, code });
});

// ---------------- render dispatcher ----------------
function render() {
  if (!state) { showScreen("home"); $("btn-leave").style.display = "none"; return; }
  $("btn-leave").style.display = "block";
  if (state.phase === "lobby") {
    showScreen("lobby");
    renderLobby();
  } else {
    showScreen("game");
    renderGame();
  }
}

$("btn-leave").addEventListener("click", () => {
  const msg = state && state.phase !== "lobby"
    ? "Leave this game? This ends the session for everyone still in the room — all scores reset, and remaining players will need to re-invite someone to fill the seat."
    : "Leave the room?";
  if (!confirm(msg)) return;
  send({ type: "leave_room" });
  localStorage.removeItem("kaadi3_session");
  myId = null;
  state = null;
  render();
});

// ---------------- lobby ----------------
function renderLobby() {
  $("lobby-code").textContent = state.code;
  const wrap = $("lobby-players");
  wrap.innerHTML = "";
  state.players.forEach((p) => {
    const d = document.createElement("div");
    d.className = "lobby-player-chip" + (p.isHost ? " host" : "");
    d.textContent = p.name + (p.isYou ? " (you)" : "");
    wrap.appendChild(d);
  });
  const need = state.numPlayers - state.players.length;
  const btn = $("btn-start");
  if (state.youAreHost) {
    btn.style.display = "inline-block";
    if (need > 0) {
      btn.disabled = true;
      btn.textContent = `Waiting for ${need} more player${need === 1 ? "" : "s"}…`;
    } else {
      btn.disabled = false;
      btn.textContent = `Start Game (${state.numPlayers} players, ${state.numDecks} deck${state.numDecks > 1 ? "s" : ""})`;
    }
  } else {
    btn.style.display = "none";
  }
  const ctrl = $("lobby-host-controls");
  let hint = ctrl.querySelector(".hint");
  if (!state.youAreHost) {
    if (!hint) {
      hint = document.createElement("div");
      hint.className = "hint";
      hint.style.fontSize = "14px";
      hint.style.opacity = "0.8";
      ctrl.appendChild(hint);
    }
    hint.textContent = need > 0 ? `Waiting for ${need} more player${need === 1 ? "" : "s"} to join…` : "Waiting for host to start…";
  } else if (hint) {
    hint.remove();
  }
}
$("btn-start").addEventListener("click", () => send({ type: "start_game" }));

// ---------------- game screen ----------------
function seatName(seat) {
  const p = state.players.find((x) => x.seat === seat);
  return p ? p.name : "?";
}
function seatPlayer(seat) {
  return state.players.find((x) => x.seat === seat);
}

function cardLabel(card) {
  return `${card.rank}${SUIT_SYMBOL[card.suit]}`;
}
function cardColorClass(suit) {
  return RED_SUITS.has(suit) ? "suit-red" : "suit-black";
}
function cardFaceHTML(card) {
  const sym = SUIT_SYMBOL[card.suit];
  return `
    <div class="card-corner tl">${card.rank}<br>${sym}</div>
    <div class="card-pip">${sym}</div>
    <div class="card-corner br">${card.rank}<br>${sym}</div>
    ${card.points ? `<div class="card-pts">${card.points}</div>` : ""}
  `;
}

let lastTrickKeys = new Set();
let lastHandCount = -1;
let lastResolvedKey = null;

const AVATAR_COLORS = [
  "#c0392b", "#2980b9", "#8e44ad", "#16a085",
  "#d35400", "#27ae60", "#2c5f8a", "#a8325e",
];
function avatarColor(name) {
  let hash = 0;
  for (let i = 0; i < name.length; i++) hash = (hash * 31 + name.charCodeAt(i)) >>> 0;
  return AVATAR_COLORS[hash % AVATAR_COLORS.length];
}

function seatPos(seat, mySeat, n, rx, ry) {
  const rel = (seat - mySeat + n) % n;
  const angle = 90 + rel * (360 / n); // degrees, 90 = bottom (you)
  const rad = (angle * Math.PI) / 180;
  return { x: 50 + rx * Math.cos(rad), y: 50 + ry * Math.sin(rad), angle };
}

function renderGame() {
  const r = state.round || {};
  const mySeat = state.you.seat;

  // topbar left: dealer + your team hint
  let left = `Dealer: ${seatName(state.dealerSeat)}`;
  if (r.teamOfYou) left += ` · Your team: ${r.teamOfYou === "caller" ? "Caller's" : "Opposition"}`;
  $("dealer-caller-info").textContent = left;

  $("phase-banner").textContent = phaseBannerText(r);

  renderPowerBadge(r);
  renderPartnerBadge(r);
  renderTable(r, mySeat);
  renderActionPanel(r, mySeat);
  renderHand(r, mySeat);
  renderScoreTable();
}

function phaseBannerText(r) {
  switch (r.phase) {
    case "bidding": {
      const hi = r.highestBid ? `${r.highestBid} (${seatName(r.highestBidder)})` : "none yet";
      return `Bidding — highest: ${hi} — ${seatName(r.turnSeat)}'s turn`;
    }
    case "power_color":
      return `${seatName(r.callerSeat)} (C) called ${r.bidAmount} — choosing power color…`;
    case "partner_select":
      return `${seatName(r.callerSeat)} (C) — power ${SUIT_SYMBOL[r.powerColor]} — choosing partner${r.partnersNeeded > 1 ? "s" : ""}…`;
    case "playing":
      return `Trick ${r.trickNumber} — power ${SUIT_SYMBOL[r.powerColor]} — ${seatName(r.turnSeat)}'s turn`;
    case "round_end":
      return "Round over";
    default:
      return "";
  }
}

function renderPowerBadge(r) {
  const badge = $("power-badge");
  if (!r.powerColor) {
    badge.style.display = "none";
    return;
  }
  badge.style.display = "block";
  const el = $("power-badge-suit");
  el.textContent = SUIT_SYMBOL[r.powerColor];
  el.className = "power-badge-suit " + cardColorClass(r.powerColor);
}

function renderPartnerBadge(r) {
  const badge = $("partner-badge");
  if (!r.partnerRequests || r.partnerRequests.length === 0) {
    badge.style.display = "none";
    return;
  }
  badge.style.display = "block";
  const cont = $("partner-badge-cards");
  cont.innerHTML = "";
  r.partnerRequests.forEach((p) => {
    const el = document.createElement("div");
    el.className = "mini-card " + cardColorClass(p.suit);
    let txt = `${p.rank}${SUIT_SYMBOL[p.suit]}`;
    if (state.numDecks === 2) txt += p.occurrence === 2 ? " (2nd)" : " (1st)";
    if (p.revealed) txt += ` = ${seatPlayer(p.ownerSeat) ? seatPlayer(p.ownerSeat).name : "?"}`;
    el.textContent = txt;
    cont.appendChild(el);
  });
}

function renderTable(r, mySeat) {
  const area = $("table-area");
  area.innerHTML = "";
  const n = state.numPlayers;

  // detect a trick that just resolved (table cleared, a fresh lastTrick to show)
  let justResolvedWinner = null;
  if (r.lastTrick && (r.trickCards || []).length === 0) {
    const resolvedKey = "t" + (r.trickNumber - 1) + "-" + r.lastTrick.winner_seat + "-" +
      r.lastTrick.cards.map((c) => c.card.id).join(",");
    if (resolvedKey !== lastResolvedKey) {
      lastResolvedKey = resolvedKey;
      justResolvedWinner = r.lastTrick.winner_seat;
    }
  }

  const ring = document.createElement("div");
  ring.className = "seat-ring";
  for (let seat = 0; seat < n; seat++) {
    const { x, y } = seatPos(seat, mySeat, n, 42, 38);
    const p = seatPlayer(seat);
    const el = document.createElement("div");
    el.className = "seat" + (r.turnSeat === seat ? " turn" : "") + (seat === justResolvedWinner ? " seat-won" : "");
    el.style.left = x + "%";
    el.style.top = y + "%";
    const pw = r.pointsWon ? r.pointsWon[seat] : undefined;
    const name = p ? p.name : "empty";
    const initial = name.charAt(0).toUpperCase();
    el.innerHTML = `
      <div class="seat-avatar" style="background:${avatarColor(name)}">
        ${initial}
        <span class="seat-status-dot ${p && p.connected ? "" : "off"}"></span>
        ${state.dealerSeat === seat ? '<span class="seat-mini-badge dealer">D</span>' : ""}
        ${r.callerSeat === seat ? '<span class="seat-mini-badge caller">C</span>' : ""}
      </div>
      <div class="seat-label">${name}${seat === mySeat ? " (you)" : ""}</div>
      ${pw !== undefined && (r.phase === "playing" || r.phase === "round_end") ? `<div class="seat-points">${pw}pt</div>` : ""}
    `;
    ring.appendChild(el);
  }
  area.appendChild(ring);

  const center = document.createElement("div");
  center.className = "trick-center";
  const currentKeys = new Set();
  (r.trickCards || []).forEach((e, idx) => {
    const c = e.card;
    const key = e.seat + ":" + c.id;
    currentKeys.add(key);
    const { x, y } = seatPos(e.seat, mySeat, n, 10, 8); // small cluster radius, % of table-area
    const rot = ((e.seat * 53) % 30) - 15; // deterministic pseudo-random tilt
    const isNew = !lastTrickKeys.has(key);

    const slot = document.createElement("div");
    slot.className = "trick-slot";
    slot.style.left = x + "%";
    slot.style.top = y + "%";
    slot.style.transform = `translate(-50%,-50%) rotate(${rot}deg)`;
    slot.style.zIndex = String(100 + idx);
    slot.innerHTML = `
      <div class="card ${cardColorClass(c.suit)}${isNew ? " card-enter" : ""}">${cardFaceHTML(c)}</div>
      <div class="who" style="transform:translateX(-50%) rotate(${-rot}deg)">${seatName(e.seat)}</div>
    `;
    center.appendChild(slot);
  });
  lastTrickKeys = currentKeys;

  if (justResolvedWinner !== null) {
    const areaRect = area.getBoundingClientRect();
    const target = seatPos(justResolvedWinner, mySeat, n, 42, 38);
    r.lastTrick.cards.forEach((e, i) => {
      const c = e.card;
      const start = seatPos(e.seat, mySeat, n, 10, 8);
      const dxPx = ((target.x - start.x) / 100) * areaRect.width;
      const dyPx = ((target.y - start.y) / 100) * areaRect.height;
      const rot = ((e.seat * 53) % 30) - 15;
      const slot = document.createElement("div");
      slot.className = "trick-slot sweep-slot";
      slot.style.left = start.x + "%";
      slot.style.top = start.y + "%";
      slot.style.setProperty("--srot", rot + "deg");
      slot.style.setProperty("--dx", dxPx + "px");
      slot.style.setProperty("--dy", dyPx + "px");
      slot.style.animationDelay = i * 40 + "ms";
      slot.style.zIndex = String(300 + i);
      slot.innerHTML = `<div class="card ${cardColorClass(c.suit)}">${cardFaceHTML(c)}</div>`;
      center.appendChild(slot);
    });
  }
  area.appendChild(center);
}

function renderActionPanel(r, mySeat) {
  const panel = $("action-panel");
  panel.innerHTML = "";

  if (r.phase === "bidding") {
    if (r.turnSeat !== mySeat) {
      panel.textContent = `Waiting for ${seatName(r.turnSeat)} to bid…`;
      return;
    }
    const next = r.highestBid ? Math.max(r.bidLo, r.highestBid + r.bidStep) : r.bidLo;
    let current = Math.min(Math.max(next, r.bidLo), r.bidHi);
    const wrap = document.createElement("div");
    wrap.style.display = "flex";
    wrap.style.gap = "8px";
    wrap.style.alignItems = "center";
    const minus = document.createElement("button");
    minus.className = "action-btn";
    minus.textContent = "−";
    const val = document.createElement("span");
    val.style.fontWeight = "800";
    val.style.minWidth = "48px";
    val.style.textAlign = "center";
    val.textContent = current;
    const plus = document.createElement("button");
    plus.className = "action-btn";
    plus.textContent = "+";
    minus.onclick = () => { current = Math.max(current - r.bidStep, r.highestBid ? r.highestBid + r.bidStep : r.bidLo); val.textContent = current; };
    plus.onclick = () => { current = Math.min(current + r.bidStep, r.bidHi); val.textContent = current; };
    const bidBtn = document.createElement("button");
    bidBtn.className = "bid-btn";
    bidBtn.textContent = `Call ${''}`;
    bidBtn.onclick = () => send({ type: "bid", amount: current });
    const updateBidLabel = () => { bidBtn.textContent = `Call ${current}`; };
    minus.addEventListener("click", updateBidLabel);
    plus.addEventListener("click", updateBidLabel);
    updateBidLabel();
    const passBtn = document.createElement("button");
    passBtn.className = "action-btn pass";
    passBtn.textContent = "Pass";
    passBtn.disabled = r.activeSeats.length <= 1;
    passBtn.onclick = () => send({ type: "pass" });
    wrap.append(minus, val, plus, bidBtn, passBtn);
    panel.appendChild(wrap);
    return;
  }

  if (r.phase === "power_color") {
    if (r.callerSeat !== mySeat) {
      panel.textContent = `${seatName(r.callerSeat)} is choosing the power color…`;
      return;
    }
    const wrap = document.createElement("div");
    wrap.style.display = "flex";
    wrap.style.gap = "10px";
    SUITS.forEach((s) => {
      const b = document.createElement("button");
      b.className = "suit-btn " + cardColorClass(s);
      b.textContent = SUIT_SYMBOL[s];
      b.onclick = () => send({ type: "select_power_color", suit: s });
      wrap.appendChild(b);
    });
    panel.appendChild(wrap);
    return;
  }

  if (r.phase === "partner_select") {
    if (r.callerSeat !== mySeat) {
      panel.textContent = `${seatName(r.callerSeat)} is choosing partner${r.partnersNeeded > 1 ? "s" : ""}…`;
      return;
    }
    const filled = r.partnerRequests ? r.partnerRequests.length : 0;
    const info = document.createElement("div");
    info.style.width = "100%";
    info.style.textAlign = "center";
    info.style.fontSize = "14px";
    info.style.marginBottom = "4px";
    info.textContent = `Pick partner ${filled + 1} of ${r.partnersNeeded} — scroll to choose color, number${state.numDecks === 2 ? ", and 1st/2nd" : ""}`;
    panel.appendChild(info);
    if (state.numDecks === 2) {
      const hint = document.createElement("div");
      hint.style.width = "100%";
      hint.style.textAlign = "center";
      hint.style.fontSize = "11px";
      hint.style.opacity = "0.65";
      hint.style.marginTop = "-2px";
      hint.style.marginBottom = "4px";
      hint.textContent = "1st/2nd = which copy was dealt out, not who plays it first — the partner reveals when whoever holds that exact copy plays it";
      panel.appendChild(hint);
    }

    const picker = document.createElement("div");
    picker.className = "partner-picker";

    const opts = r.partnerOptions || {};
    // column 1: suit
    const colSuit = document.createElement("div");
    colSuit.className = "partner-col";
    Object.keys(opts).forEach((suit) => {
      const it = document.createElement("button");
      it.className = "partner-col-item " + cardColorClass(suit) + (partnerBuild.suit === suit ? " selected" : "");
      it.textContent = `${SUIT_SYMBOL[suit]} ${SUIT_NAME[suit]}`;
      it.onclick = () => { partnerBuild.suit = suit; partnerBuild.rank = null; partnerBuild.occurrence = null; renderActionPanel(r, mySeat); };
      colSuit.appendChild(it);
    });
    picker.appendChild(colSuit);

    // column 2: rank
    const colRank = document.createElement("div");
    colRank.className = "partner-col";
    if (partnerBuild.suit && opts[partnerBuild.suit]) {
      Object.keys(opts[partnerBuild.suit]).forEach((rank) => {
        const it = document.createElement("button");
        it.className = "partner-col-item" + (partnerBuild.rank === rank ? " selected" : "");
        it.textContent = rank;
        it.onclick = () => { partnerBuild.rank = rank; partnerBuild.occurrence = state.numDecks === 2 ? null : 1; renderActionPanel(r, mySeat); };
        colRank.appendChild(it);
      });
    }
    picker.appendChild(colRank);

    // column 3: occurrence (2-deck only)
    if (state.numDecks === 2) {
      const colOcc = document.createElement("div");
      colOcc.className = "partner-col";
      if (partnerBuild.suit && partnerBuild.rank) {
        const occs = opts[partnerBuild.suit][partnerBuild.rank];
        occs.forEach((occ) => {
          const it = document.createElement("button");
          it.className = "partner-col-item" + (partnerBuild.occurrence === occ ? " selected" : "");
          it.textContent = occ === 1 ? "1st" : "2nd";
          it.onclick = () => { partnerBuild.occurrence = occ; renderActionPanel(r, mySeat); };
          colOcc.appendChild(it);
        });
      }
      picker.appendChild(colOcc);
    }
    panel.appendChild(picker);

    const confirmBtn = document.createElement("button");
    confirmBtn.className = "btn primary";
    confirmBtn.style.marginTop = "6px";
    confirmBtn.textContent = "Confirm Partner Card";
    confirmBtn.disabled = !(partnerBuild.suit && partnerBuild.rank && partnerBuild.occurrence);
    confirmBtn.onclick = () => {
      send({ type: "select_partner", suit: partnerBuild.suit, rank: partnerBuild.rank, occurrence: partnerBuild.occurrence });
      partnerBuild = { suit: null, rank: null, occurrence: null };
    };
    panel.appendChild(confirmBtn);
    return;
  }

  if (r.phase === "playing") {
    panel.textContent = r.turnSeat === mySeat ? "Your turn — tap a card" : `Waiting for ${seatName(r.turnSeat)}…`;
    return;
  }

  if (r.phase === "round_end" && r.roundResult) {
    const res = r.roundResult;
    const card = document.createElement("div");
    card.className = "round-result-card";
    const won = res.won;
    card.innerHTML = `
      <h2>${won ? "Caller's team WINS the round" : "Caller's team LOSES the round"}</h2>
      <div>${seatName(res.caller_seat)} called ${res.bid_amount}, team scored ${res.caller_points}</div>
      <div style="margin-top:8px;font-size:14px;">
        ${state.players.map((p) => `${p.name}: ${res.deltas[p.seat] >= 0 ? "+" : ""}${res.deltas[p.seat]}`).join(" &nbsp; ")}
      </div>
    `;
    if (state.youAreHost) {
      const btn = document.createElement("button");
      btn.className = "btn primary";
      btn.style.marginTop = "10px";
      btn.textContent = "Next Round";
      btn.onclick = () => send({ type: "next_round" });
      card.appendChild(document.createElement("br"));
      card.appendChild(btn);
    } else {
      const w = document.createElement("div");
      w.style.marginTop = "10px";
      w.style.fontSize = "14px";
      w.textContent = "Waiting for host to start next round…";
      card.appendChild(w);
    }
    panel.appendChild(card);
  }
}

function renderHand(r, mySeat) {
  const row = $("hand-row");
  row.innerHTML = "";
  const hand = r.hand || [];
  const legal = new Set(r.legalCardIds || []);
  const canPlay = r.phase === "playing" && r.turnSeat === mySeat;
  const isFreshDeal = hand.length > lastHandCount;
  const n = hand.length;
  const center = (n - 1) / 2;
  const angleStep = Math.min(6, 42 / Math.max(n, 1));

  // CSS `transform` never counts toward an element's scrollable overflow,
  // and overflow-x:auto forces overflow-y to behave as auto too (can't mix
  // visible/non-visible per spec) -- so the fanned/rotated cards WILL get
  // silently clipped unless the row's actual layout height already
  // contains their full rotated extent. A fixed CSS buffer can't get this
  // right across every hand size and every clamp()'d card size, so compute
  // it for real: probe the resolved card height, then work out how far the
  // most-rotated card swings around its off-center pivot.
  const probe = document.createElement("div");
  probe.className = "card";
  probe.style.visibility = "hidden";
  probe.style.position = "absolute";
  row.appendChild(probe);
  const cardH = probe.offsetHeight || 90;
  probe.remove();

  const maxOffset = n > 1 ? Math.max(center, n - 1 - center) : 0;
  const maxTy = Math.pow(maxOffset, 1.3) * 2.6;
  const maxRotRad = (maxOffset * angleStep * Math.PI) / 180;
  const pivotDist = cardH * 1.08; // matches transform-origin: 50% 108%
  const swing = pivotDist * (1 - Math.cos(maxRotRad));
  const topMargin = 10;
  row.style.minHeight = Math.ceil(topMargin + cardH + maxTy + swing + 10) + "px";

  hand.forEach((c, i) => {
    const el = document.createElement("div");
    const isLegal = canPlay && legal.has(c.id);
    const offset = i - center;
    const rot = offset * angleStep;
    const ty = topMargin + Math.pow(Math.abs(offset), 1.3) * 2.6;
    el.style.setProperty("--rot", rot + "deg");
    el.style.setProperty("--ty", ty + "px");
    el.style.zIndex = String(i);
    el.className = "card hand-card " + cardColorClass(c.suit) + (canPlay && !isLegal ? " disabled" : "") + (isFreshDeal ? " deal-in" : "");
    if (isFreshDeal) el.style.animationDelay = (i * 35) + "ms";
    else el.style.transform = `translateY(${ty}px) rotate(${rot}deg)`;
    el.innerHTML = cardFaceHTML(c);
    if (isLegal) el.onclick = () => send({ type: "play_card", cardId: c.id });
    row.appendChild(el);
  });
  lastHandCount = hand.length;

  // justify-content:center on a horizontally-scrolling flex row is a known
  // trap: when content is wider than the container, half the overflow sits
  // on the *leading* edge, which a scroll container can never reach
  // (scrollLeft can't go negative) -- those cards become permanently
  // invisible. Only center when everything actually fits; otherwise
  // left-align so every card is reachable by scrolling.
  const overflowing = row.scrollWidth > row.clientWidth;
  row.style.justifyContent = overflowing ? "flex-start" : "center";
  row.classList.toggle("scrollable", overflowing);

  // Safety net on top of the analytical sizing above: real devices/browsers
  // can differ in clamp()/font metrics, sub-pixel rounding, or (iOS Safari
  // especially) a dynamic toolbar changing the viewport after layout --
  // none of which a single synthetic test environment can fully rule out.
  // After layout actually settles, measure for real and grow the row if
  // anything still pokes past its bottom edge, rather than trusting the
  // math alone.
  const settleDelay = isFreshDeal ? 420 : 30;
  const handSizeAtSchedule = hand.length;
  setTimeout(() => {
    if (lastHandCount !== handSizeAtSchedule) return; // a newer hand has since rendered
    const rowRect = row.getBoundingClientRect();
    if (rowRect.height === 0) return; // not visible right now
    let maxBottom = -Infinity;
    row.querySelectorAll(".hand-card").forEach((el) => {
      const cr = el.getBoundingClientRect();
      if (cr.bottom > maxBottom) maxBottom = cr.bottom;
    });
    const overBy = maxBottom - rowRect.bottom;
    if (overBy > 0.5) {
      const current = parseFloat(row.style.minHeight) || 0;
      row.style.minHeight = Math.ceil(current + overBy + 6) + "px";
    }
  }, settleDelay);
}

function renderScoreTable() {
  const wrap = $("score-table");
  const rows = state.players.slice().sort((a, b) => a.seat - b.seat);
  wrap.innerHTML = rows.map((p) => {
    const val = p.sessionScore;
    const initial = p.name.charAt(0).toUpperCase();
    return `
      <div class="score-chip">
        <span class="chip-avatar" style="background:${avatarColor(p.name)}">${initial}</span>
        <span class="chip-name">${p.name}${p.isHost ? " ★" : ""}</span>
        <span class="chip-value${val < 0 ? " negative" : ""}">${val >= 0 ? "+" : ""}${val}</span>
      </div>
    `;
  }).join("");
}
$("score-table-toggle").addEventListener("click", () => {
  $("score-table").classList.toggle("open");
});

showScreen("home");
connect();
