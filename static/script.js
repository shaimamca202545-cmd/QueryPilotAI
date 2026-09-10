(() => {
  "use strict";

  const chatScroll = document.getElementById("chatScroll");
  const welcomeCard = document.getElementById("welcomeCard");
  const messageInput = document.getElementById("messageInput");
  const sendBtn = document.getElementById("sendBtn");
  const newQueryBtn = document.getElementById("newQueryBtn");
  const clearChatBtn = document.getElementById("clearChatBtn");
  const themeToggle = document.getElementById("themeToggle");
  const sidebarToggle = document.getElementById("sidebarToggle");
  const sidebar = document.getElementById("sidebar");
  const connDot = document.getElementById("connDot");
  const connLabel = document.getElementById("connLabel");
  const connStatus = document.getElementById("connStatus");
  const dbName = document.getElementById("dbName");
  const sidebarDbName = document.getElementById("sidebarDbName");
  const sidebarDbMeta = document.getElementById("sidebarDbMeta");
  const tableList = document.getElementById("tableList");
  const tableCount = document.getElementById("tableCount");
  const exampleChips = document.getElementById("exampleChips");

  const userMsgTemplate = document.getElementById("userMsgTemplate");
  const aiMsgTemplate = document.getElementById("aiMsgTemplate");

  const SQL_KEYWORDS = [
    "SELECT", "FROM", "WHERE", "AND", "OR", "NOT", "JOIN", "LEFT", "RIGHT", "INNER",
    "OUTER", "ON", "GROUP BY", "ORDER BY", "HAVING", "LIMIT", "OFFSET", "AS", "INSERT",
    "INTO", "VALUES", "UPDATE", "SET", "DELETE", "CREATE", "TABLE", "ALTER", "ADD",
    "DROP", "TRUNCATE", "DISTINCT", "COUNT", "SUM", "AVG", "MIN", "MAX", "LIKE",
    "BETWEEN", "IN", "IS", "NULL", "DESC", "ASC", "UNION", "EXISTS", "CASE", "WHEN",
    "THEN", "ELSE", "END",
  ];

  let sessionId = localStorage.getItem("qp_session_id") || null;

  // ---------- session / storage ----------
  function ensureSession() {
    if (!sessionId) {
      sessionId = crypto.randomUUID ? crypto.randomUUID() : String(Date.now());
      localStorage.setItem("qp_session_id", sessionId);
    }
  }
  ensureSession();

  // ---------- theme ----------
  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem("qp_theme", theme);
  }
  (function initTheme() {
    const saved = localStorage.getItem("qp_theme");
    const prefersLight = window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches;
    applyTheme(saved || (prefersLight ? "light" : "dark"));
  })();
  themeToggle.addEventListener("click", () => {
    const current = document.documentElement.getAttribute("data-theme");
    applyTheme(current === "dark" ? "light" : "dark");
  });

  // ---------- sidebar toggle (mobile) ----------
  sidebarToggle.addEventListener("click", () => sidebar.classList.toggle("collapsed"));

  // ---------- health polling ----------
  async function pollHealth() {
    try {
      const res = await fetch("/health");
      const data = await res.json();
      dbName.textContent = data.database || "—";
      if (data.mysql_connected && data.ollama_connected) {
        setStatus("online", "Connected");
      } else if (data.mysql_connected && !data.ollama_connected) {
        setStatus("offline", "Ollama offline");
      } else if (!data.mysql_connected) {
        setStatus("offline", "MySQL offline");
      } else {
        setStatus("offline", "Degraded");
      }
    } catch (e) {
      setStatus("offline", "Unreachable");
    }
  }
  function setStatus(state, label) {
    connDot.className = "status-dot " + state;
    connLabel.textContent = label;
    connStatus.title = label;
  }
  pollHealth();
  setInterval(pollHealth, 15000);

  // ---------- schema sidebar ----------
  async function loadSchema() {
    try {
      const res = await fetch("/schema");
      if (!res.ok) throw new Error("schema fetch failed");
      const data = await res.json();
      sidebarDbName.textContent = data.database || "—";
      const totalCols = data.tables.reduce((a, t) => a + t.columns.length, 0);
      sidebarDbMeta.textContent = `${data.tables.length} table${data.tables.length === 1 ? "" : "s"} · ${totalCols} columns`;
      tableCount.textContent = data.tables.length;
      renderTableList(data.tables);
    } catch (e) {
      sidebarDbMeta.textContent = "Could not load schema";
      tableList.innerHTML = `<div class="result-empty">Schema unavailable. Check the MySQL connection.</div>`;
    }
  }

  function renderTableList(tables) {
    tableList.innerHTML = "";
    if (!tables.length) {
      tableList.innerHTML = `<div class="result-empty">No tables found.</div>`;
      return;
    }
    tables.forEach((t) => {
      const item = document.createElement("div");
      item.className = "table-item";

      const head = document.createElement("div");
      head.className = "table-item-head";
      head.innerHTML = `
        <svg class="chevron" width="12" height="12" viewBox="0 0 24 24" fill="none"><path d="M9 6l6 6-6 6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
        <span class="table-item-name">${escapeHtml(t.name)}</span>
        <span class="table-item-rows">${t.row_count != null ? t.row_count.toLocaleString() + " rows" : ""}</span>
      `;
      head.addEventListener("click", () => item.classList.toggle("open"));

      const cols = document.createElement("div");
      cols.className = "table-item-cols";
      t.columns.forEach((c) => {
        const row = document.createElement("div");
        row.className = "col-row";
        const fk = t.foreign_keys.find((f) => f.column === c.name);
        row.innerHTML = `
          <span class="col-name">${escapeHtml(c.name)}</span>
          <span class="col-type">${escapeHtml(c.type)}</span>
          ${c.primary_key ? '<span class="tag tag-pk">PK</span>' : ""}
          ${fk ? `<span class="tag tag-fk" title="References ${escapeHtml(fk.references_table)}.${escapeHtml(fk.references_column)}">FK</span>` : ""}
        `;
        cols.appendChild(row);
      });

      item.appendChild(head);
      item.appendChild(cols);
      tableList.appendChild(item);
    });
  }
  loadSchema();

  // ---------- chat ----------
  function scrollToBottom() {
    chatScroll.scrollTop = chatScroll.scrollHeight;
  }

  function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str == null ? "" : String(str);
    return div.innerHTML;
  }

  function highlightSql(sql) {
    let escaped = escapeHtml(sql);
    const sorted = [...SQL_KEYWORDS].sort((a, b) => b.length - a.length);
    sorted.forEach((kw) => {
      const re = new RegExp(`\\b${kw.replace(" ", "\\s+")}\\b`, "gi");
      escaped = escaped.replace(re, (m) => `\u0001${m}\u0002`);
    });
    escaped = escaped.replace(/\u0001(.*?)\u0002/g, '<span class="sql-kw">$1</span>');
    return escaped;
  }

  function addUserMessage(text) {
    const node = userMsgTemplate.content.cloneNode(true);
    node.querySelector(".msg-bubble").textContent = text;
    chatScroll.appendChild(node);
    scrollToBottom();
  }

  function addLoadingMessage() {
    const node = aiMsgTemplate.content.cloneNode(true);
    const wrapper = document.createElement("div");
    // re-select from the fragment before it's detached
    const bubble = node.querySelector(".msg-bubble");
    bubble.classList.add("is-loading");
    bubble.innerHTML = `Thinking through your request <span class="typing-dots"><span></span><span></span><span></span></span>`;
    const msgEl = node.querySelector(".msg");
    chatScroll.appendChild(node);
    scrollToBottom();
    return chatScroll.lastElementChild;
  }

  function statementBadgeClass(type) {
    const writeTypes = ["INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP", "TRUNCATE", "REPLACE"];
    return writeTypes.includes((type || "").toUpperCase()) ? "write" : "";
  }

  function buildSqlPanel(sql, statementType) {
    const panel = document.createElement("div");
    panel.className = "sql-panel";
    panel.innerHTML = `
      <div class="sql-panel-head">
        <svg class="chevron" width="11" height="11" viewBox="0 0 24 24" fill="none"><path d="M9 6l6 6-6 6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
        <span class="sql-panel-title">Generated SQL</span>
        <span class="sql-panel-badge ${statementBadgeClass(statementType)}">${escapeHtml(statementType || "SQL")}</span>
      </div>
      <div class="sql-panel-body">
        <pre class="sql-code">${highlightSql(sql)}</pre>
      </div>
    `;
    panel.querySelector(".sql-panel-head").addEventListener("click", () => panel.classList.toggle("open"));
    return panel;
  }

  function buildResultPanel(data) {
    const panel = document.createElement("div");
    panel.className = "result-panel";

    const isSelect = (data.statement_type || "").toUpperCase() === "SELECT" || (data.columns && data.columns.length);
    const metaBits = [`${data.row_count} row${data.row_count === 1 ? "" : "s"}`, `${data.elapsed_ms} ms`];
    if (data.truncated) metaBits.push("truncated");

    const head = document.createElement("div");
    head.className = "result-panel-head";
    head.innerHTML = `<span>Query Result</span><span class="meta">${metaBits.join(" · ")}</span>`;
    panel.appendChild(head);

    if (isSelect && data.columns && data.columns.length) {
      if (!data.rows.length) {
        const empty = document.createElement("div");
        empty.className = "result-empty";
        empty.textContent = "No rows returned.";
        panel.appendChild(empty);
      } else {
        const wrap = document.createElement("div");
        wrap.className = "result-table-wrap";
        const table = document.createElement("table");
        table.className = "result-table";
        const thead = document.createElement("thead");
        thead.innerHTML = `<tr>${data.columns.map((c) => `<th>${escapeHtml(c)}</th>`).join("")}</tr>`;
        const tbody = document.createElement("tbody");
        data.rows.forEach((row) => {
          const tr = document.createElement("tr");
          tr.innerHTML = data.columns.map((c) => `<td>${escapeHtml(row[c])}</td>`).join("");
          tbody.appendChild(tr);
        });
        table.appendChild(thead);
        table.appendChild(tbody);
        wrap.appendChild(table);
        panel.appendChild(wrap);
      }
    } else {
      const affected = document.createElement("div");
      affected.className = "result-affected";
      affected.textContent = `${data.row_count} row${data.row_count === 1 ? "" : "s"} affected.`;
      panel.appendChild(affected);
    }
    return panel;
  }

  function replaceLoadingWithResponse(loadingEl, data) {
    const bubble = loadingEl.querySelector(".msg-bubble");
    bubble.classList.remove("is-loading");
    if (data.error && !data.sql) {
      bubble.classList.add("is-error");
    }
    bubble.textContent = data.reply;

    const extras = loadingEl.querySelector(".msg-extras");
    if (data.sql) {
      extras.appendChild(buildSqlPanel(data.sql, data.statement_type));
    }
    if (data.sql && !data.error) {
      extras.appendChild(buildResultPanel(data));
    }
    scrollToBottom();
  }

  async function sendMessage(text) {
    if (!text.trim()) return;
    welcomeCard.style.display = "none";
    addUserMessage(text);
    messageInput.value = "";
    autosize();
    sendBtn.disabled = true;

    const loadingEl = addLoadingMessage();

    try {
      const res = await fetch("/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, session_id: sessionId }),
      });
      const data = await res.json();
      if (data.session_id) {
        sessionId = data.session_id;
        localStorage.setItem("qp_session_id", sessionId);
      }
      replaceLoadingWithResponse(loadingEl, data);
    } catch (e) {
      const bubble = loadingEl.querySelector(".msg-bubble");
      bubble.classList.remove("is-loading");
      bubble.classList.add("is-error");
      bubble.textContent = "I couldn't reach the server. Please check that the app is running and try again.";
    } finally {
      sendBtn.disabled = false;
      // schema may have changed (DDL/DML) - refresh sidebar counts quietly
      loadSchema();
    }
  }

  // ---------- composer behaviour ----------
  function autosize() {
    messageInput.style.height = "auto";
    messageInput.style.height = Math.min(messageInput.scrollHeight, 160) + "px";
  }
  messageInput.addEventListener("input", autosize);
  messageInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage(messageInput.value);
    }
  });
  sendBtn.addEventListener("click", () => sendMessage(messageInput.value));

  exampleChips.addEventListener("click", (e) => {
    const chip = e.target.closest(".chip");
    if (!chip) return;
    sendMessage(chip.textContent);
  });

  // ---------- New Query / Clear Chat ----------
  function resetConversation() {
    if (sessionId) {
      fetch(`/reset/${sessionId}`, { method: "POST" }).catch(() => {});
    }
    sessionId = crypto.randomUUID ? crypto.randomUUID() : String(Date.now());
    localStorage.setItem("qp_session_id", sessionId);
    Array.from(chatScroll.querySelectorAll(".msg")).forEach((el) => el.remove());
    welcomeCard.style.display = "";
  }
  newQueryBtn.addEventListener("click", resetConversation);
  clearChatBtn.addEventListener("click", resetConversation);

  messageInput.focus();
})();
