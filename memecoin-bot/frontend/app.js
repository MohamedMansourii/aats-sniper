const REFRESH_MS = 5000;

async function getJSON(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`${path}: ${res.status}`);
  return res.json();
}

function fmtUptime(s) {
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  return `${h}h ${m}m ${sec}s`;
}

function shorten(addr) {
  if (!addr || addr.length < 12) return addr || "";
  return `${addr.slice(0, 4)}…${addr.slice(-4)}`;
}

function fmtUsd(n) {
  if (!n) return "—";
  if (n >= 1e6) return `$${(n / 1e6).toFixed(2)}M`;
  if (n >= 1e3) return `$${(n / 1e3).toFixed(1)}k`;
  return `$${n.toFixed(0)}`;
}

async function refresh() {
  try {
    const m = await getJSON("/api/metrics");

    document.getElementById("mode-badge").textContent = m.mode + (m.dry_run ? " · dry-run" : " · LIVE");
    const banner = document.getElementById("dry-run-banner");
    banner.classList.toggle("hidden", !m.dry_run);

    document.getElementById("uptime").textContent = fmtUptime(m.uptime_seconds);
    document.getElementById("found").textContent = m.tokens_found;
    document.getElementById("filtered").textContent = m.tokens_filtered;
    document.getElementById("candidates-count").textContent = m.candidates;
    document.getElementById("trades").textContent = m.trades_today;
    document.getElementById("winrate").textContent = `${(m.win_rate * 100).toFixed(0)}%`;

    const pnl = document.getElementById("pnl");
    pnl.textContent = m.total_pnl_sol.toFixed(3);
    pnl.className = "value " + (m.total_pnl_sol >= 0 ? "pos" : "neg");

    const breaker = document.getElementById("breaker");
    breaker.textContent = m.risk && m.risk.paused ? "PAUSED" : "OK";
    breaker.className = "value " + (m.risk && m.risk.paused ? "neg" : "pos");

    const rows = await getJSON("/api/candidates");
    const tbody = document.querySelector("#candidates tbody");
    tbody.innerHTML = "";
    for (const c of rows) {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${new Date(c.created_at).toLocaleTimeString()}</td>
        <td>${c.kol_name || "—"}</td>
        <td class="mono">${shorten(c.token_address)}</td>
        <td>${fmtUsd(c.market_cap_usd)}</td>
        <td>${c.safety_score}</td>
        <td>${c.win_probability.toFixed(0)}</td>
        <td class="status-${c.status}">${c.status}</td>
        <td>${c.decision_reason || ""}</td>`;
      tbody.appendChild(tr);
    }
  } catch (err) {
    console.error(err);
  }
}

refresh();
setInterval(refresh, REFRESH_MS);
