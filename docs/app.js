/* TIDE — pull payment stream board. Reads the app's global state from the
   TestNet indexer once appId > 0 in deploy.json. Pool balance comes from
   the indexer account endpoint when appAddress is configured. Live first;
   on feed failure falls back to the last good snapshot (STALE) rather
   than guessing. TestNet only. Read-only. No wallet. No keys. */
(() => {
  const INDEXER = "https://testnet-idx.algonode.cloud";
  const ALGOD = "https://testnet-api.algonode.cloud";
  const EXPLORER = "https://testnet.explorer.perawallet.app/application/";
  const CONTRACT_SRC =
    "https://github.com/corvid-agent/tide/blob/main/smart_contracts/tide/contract.py";
  const DEFAULT_KEEPER = 769891898;
  const MIN_BALANCE_FLOOR = 100000; // uALGO floor tick() reserves on the app
  const REFRESH_MS = 30000;
  const SNAPSHOT_KEY = "tide:snapshot";

  function b64utf8(b64) {
    try { return atob(b64); } catch { return ""; }
  }

  function b64ToHex(b64) {
    try {
      const bin = atob(b64);
      let hex = "";
      for (let i = 0; i < bin.length; i++) {
        hex += bin.charCodeAt(i).toString(16).padStart(2, "0");
      }
      return hex;
    } catch {
      return "";
    }
  }

  function readGlobal(state, name) {
    if (!Array.isArray(state)) return null;
    for (const kv of state) {
      if (b64utf8(kv.key) !== name) continue;
      if (kv.value && kv.value.type === 2) return { kind: "uint", v: kv.value.uint };
      if (kv.value && kv.value.type === 1) return { kind: "bytes", v: kv.value.bytes };
      return null;
    }
    return null;
  }

  async function fetchJson(url, noStore) {
    const opts = { headers: { Accept: "application/json" } };
    if (noStore) opts.cache = "no-store";
    const res = await fetch(url, opts);
    if (!res.ok) throw new Error(url + " " + res.status);
    return res.json();
  }

  function flaps(el, text) {
    el.replaceChildren();
    for (const ch of String(text)) {
      const d = document.createElement("span");
      d.className = "flap" + (ch === " " ? " blank" : "");
      d.textContent = ch === " " ? " " : ch;
      el.appendChild(d);
    }
  }

  function setStatus(word, cls, subHtml) {
    const el = document.getElementById("status");
    el.className = "flaps big " + cls;
    flaps(el, word.toUpperCase());
    document.getElementById("subhead").innerHTML = subHtml;
    document.title = "TIDE — " + word.toUpperCase();
  }

  const STAT_IDS = [
    "stat-claimable", "stat-drip", "stat-matured", "stat-claimed",
    "stat-beneficiary", "stat-pool", "stat-round", "stat-keeper",
  ];

  function fillStats(map) {
    for (const id of STAT_IDS) {
      flaps(document.getElementById(id), map[id] || "—");
    }
  }

  function micro(n) {
    return String(n) + " µA";
  }

  function shortHex(hex) {
    if (!hex) return "—";
    return hex.length > 18 ? hex.slice(0, 8) + "…" + hex.slice(-8) : hex;
  }

  function saveSnapshot(snap) {
    try {
      localStorage.setItem(SNAPSHOT_KEY, JSON.stringify(snap));
    } catch { /* storage unavailable; live-only then */ }
  }

  function loadSnapshot() {
    try {
      const raw = localStorage.getItem(SNAPSHOT_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch {
      return null;
    }
  }

  function renderSnapshot(snap) {
    const ageMin = Math.max(0, Math.round((Date.now() - snap.ts) / 60000));
    setStatus("STALE", "gate",
      "feed unreachable · last good read " + ageMin + " min ago: " +
      snap.word + (snap.subText ? " · " + snap.subText : ""));
    fillStats(snap.stats || {});
  }

  let cfgPromise = null;
  function loadConfig() {
    if (!cfgPromise) {
      cfgPromise = fetchJson("./deploy.json", true).then((c) => ({
        appId: Number(c.appId) || 0,
        appAddress: c.appAddress || "",
        keeper: Number(c.keeperAppId) || DEFAULT_KEEPER,
        network: c.network || "testnet",
        notes: c.notes || "",
      }));
    }
    return cfgPromise;
  }

  async function tickBoard() {
    let cfg;
    try {
      cfg = await loadConfig();
    } catch (e) {
      setStatus("FEED DOWN", "down",
        "deploy.json unreadable · showing nothing rather than guessing");
      fillStats({});
      return;
    }
    document.getElementById("keeper-meta").textContent =
      cfg.network + " · Arcron keeper " + cfg.keeper;

    if (cfg.appId <= 0) {
      setStatus("NOT DEPLOYED", "gate",
        'contract exists as <a href="' + CONTRACT_SRC + '">source</a> only' +
        " · lights up after TestNet deploy + set_keeper + set_beneficiary + set_drip + fund + Arcron registration");
      fillStats({ "stat-keeper": String(cfg.keeper) });
      return;
    }

    let round, gs, pool = null;
    try {
      const status = await fetchJson(ALGOD + "/v2/status");
      round = status["last-round"];
      const app = await fetchJson(INDEXER + "/v2/applications/" + cfg.appId);
      const params = (app.application && app.application.params) || app.params || {};
      gs = params["global-state"];
      if (cfg.appAddress) {
        const acct = await fetchJson(INDEXER + "/v2/accounts/" + cfg.appAddress);
        pool = (acct.account && typeof acct.account.amount === "number")
          ? acct.account.amount : null;
      }
    } catch (e) {
      const snap = loadSnapshot();
      if (snap && snap.appId === cfg.appId) {
        renderSnapshot(snap);
      } else {
        setStatus("FEED DOWN", "down",
          "indexer unreachable · no prior snapshot · showing nothing rather than guessing");
        fillStats({ "stat-keeper": String(cfg.keeper) });
      }
      return;
    }

    const keeperApp = readGlobal(gs, "keeper_app");
    const drip = readGlobal(gs, "drip");
    const matured = readGlobal(gs, "matured");
    const claimedTotal = readGlobal(gs, "claimed_total");
    const beneficiary = readGlobal(gs, "beneficiary");

    const nDrip = drip && drip.kind === "uint" ? drip.v : 0;
    const nMatured = matured && matured.kind === "uint" ? matured.v : 0;
    const nClaimed = claimedTotal && claimedTotal.kind === "uint" ? claimedTotal.v : 0;
    const benefHex = beneficiary && beneficiary.kind === "bytes"
      ? b64ToHex(beneficiary.v) : "";
    const benefSet = benefHex !== "" && !/^0+$/.test(benefHex);
    const claimable = nMatured * nDrip;
    const coversNext = pool !== null
      ? (pool - MIN_BALANCE_FLOOR) >= nDrip * (nMatured + 1)
      : null;

    const stats = {
      "stat-claimable": micro(claimable),
      "stat-drip": nDrip > 0 ? micro(nDrip) : "—",
      "stat-matured": String(nMatured),
      "stat-claimed": micro(nClaimed),
      "stat-beneficiary": benefSet ? shortHex(benefHex) : "—",
      "stat-pool": pool !== null ? micro(pool) : "—",
      "stat-round": String(round),
      "stat-keeper": keeperApp ? String(keeperApp.v) : "—",
    };
    fillStats(stats);

    const appLink = 'app <a href="' + EXPLORER + cfg.appId + '">' + cfg.appId + "</a>";
    let word, cls, subText;
    if (!keeperApp || keeperApp.v === 0) {
      word = "NO KEEPER"; cls = "gate";
      subText = appLink + " is live but set_keeper has not run yet";
    } else if (!benefSet) {
      word = "NO BENEFICIARY"; cls = "gate";
      subText = appLink + " keeper wired · owner has not named a beneficiary yet" +
        " · tick() fail-softs until then";
    } else if (coversNext === false) {
      word = "POOL LOW"; cls = "down";
      subText = appLink + " pool cannot cover the next drip" +
        " · tick() returns 0 until fund() tops it up · " + micro(claimable) + " still claimable";
    } else {
      word = "FLOWING"; cls = "live";
      subText = appLink + " matures " + micro(nDrip) + " per keeper tick · " +
        micro(claimable) + " claimable now" +
        (coversNext === null ? " · pool balance not configured" : "");
    }
    setStatus(word, cls, subText);

    saveSnapshot({
      appId: cfg.appId,
      ts: Date.now(),
      word: word,
      subText: subText.replace(/<[^>]*>/g, ""),
      stats: stats,
    });
  }

  tickBoard();
  setInterval(tickBoard, REFRESH_MS);
})();
