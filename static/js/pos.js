(function () {
  "use strict";

  // All money is whole paise (integers). Percentages are "basis points": 10% = 1000.
  // compute() must follow the same steps as compute_bill() in pos/services.py.
  const state = {
    items: [],        // { id, name, sku, price, mrp, gstBp, qty, stock, track, dKind, dValue, promos }
    selected: null,
    customer: null,   // null = Walk-in customer
    bill: { kind: null, value: 0 },
    token: null,
    busy: false,
  };

  const $ = (id) => document.getElementById(id);
  const app = document.querySelector(".pos-app");
  const body = $("cartBody");
  const empty = $("cartEmpty");
  const searchInput = $("posSearch");
  const resultsBox = $("posResults");
  const noteBar = $("posNote");
  const SEARCH_URL = searchInput.dataset.url;
  const CHECKOUT_URL = app.dataset.checkoutUrl;
  const CUSTOMER_URL = app.dataset.customerUrl;
  const PROMO_URL = app.dataset.promoUrl;
  const CSRF = app.dataset.csrf;
  const MAX_DISCOUNT = app.dataset.maxDiscount;   // empty for admins

  // ---------- Helpers ----------
  const divRound = (a, b) => Math.floor((2 * a + b) / (2 * b));
  const inr = (p) => "₹ " + (p / 100).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const hStr = (n) => Math.floor(n / 100) + "." + String(n % 100).padStart(2, "0");   // 1250 -> "12.50"
  const fmtQty = (n) => String(Math.round(n * 1000) / 1000);

  // "12.5" -> 1250. Returns null when the text is not a valid amount with up to 2 decimals.
  function toHundredths(text) {
    const s = String(text).trim();
    if (s === "") return 0;
    if (!/^\d+(\.\d{1,2})?$/.test(s)) return null;
    const parts = s.split(".");
    return parseInt(parts[0], 10) * 100 + parseInt(((parts[1] || "") + "00").slice(0, 2), 10);
  }

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }

  function makeToken() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    return Date.now().toString(36) + Math.random().toString(36).slice(2, 12);
  }

  // ---------- Message bar ----------
  let noteTimer = null;
  function showNote(text) {
    clearTimeout(noteTimer);
    if (!text) { noteBar.hidden = true; noteBar.textContent = ""; return; }
    noteBar.textContent = text;
    noteBar.hidden = false;
    noteTimer = setTimeout(() => showNote(""), 6000);
  }

  // ---------- The money maths ----------
  function applyDisc(base, kind, value) {
    let d = 0;
    if (kind === "percent") d = divRound(base * value, 10000);
    else if (kind === "amount") d = value;
    return Math.min(d, base);
  }

  // The best running promotion for a line (they never stack). Same steps as best_discount() on the server.
  // A rule: { name, kind: "percent"|"amount", value (basis points or paise per unit), min (milli-units, 0 = none) }
  function promoFor(it, gross) {
    let best = { amount: 0, name: "" };
    (it.promos || []).forEach((r) => {
      if (r.min && it.qty * 1000 < r.min) return;
      let d = r.kind === "percent" ? divRound(gross * r.value, 10000) : divRound(r.value * it.qty * 1000, 1000);
      d = Math.min(d, gross);
      if (d > best.amount) best = { amount: d, name: r.name };
    });
    return best;
  }

  function promoLabel(r) {
    const v = r.kind === "percent" ? (r.value / 100) + "% off" : "₹" + (r.value / 100).toFixed(2) + " off each";
    return r.min ? v + " (buy " + fmtQty(r.min / 1000) + "+)" : v;
  }

  function compute() {
    const rows = state.items.map((it) => {
      const gross = it.price * it.qty;
      const promo = promoFor(it, gross);                                      // the promotion comes off first
      const lineDisc = applyDisc(gross - promo.amount, it.dKind, it.dValue);  // then the cashier's discount
      return { it, gross, promo: promo.amount, promoName: promo.name, lineDisc, net: gross - promo.amount - lineDisc, share: 0 };
    });
    const base = rows.reduce((s, r) => s + r.net, 0);
    const billDisc = applyDisc(base, state.bill.kind, state.bill.value);

    if (billDisc && base) {
      let given = 0;
      rows.forEach((r) => { r.share = Math.floor((billDisc * r.net) / base); given += r.share; });
      let left = billDisc - given;
      let i = 0;
      while (left > 0) {
        const r = rows[i % rows.length];
        if (r.share < r.net) { r.share += 1; left -= 1; }
        i += 1;
      }
    }

    let subtotal = 0, discount = 0, tax = 0, total = 0;
    rows.forEach((r) => {
      r.total = r.gross - r.promo - r.lineDisc - r.share;
      r.tax = divRound(r.total * r.it.gstBp, 10000 + r.it.gstBp);
      subtotal += r.gross; discount += r.promo + r.lineDisc + r.share; tax += r.tax; total += r.total;
    });
    return { rows, subtotal, discount, tax, total };
  }

  // ---------- Cart ----------
  function touch() { state.token = null; }   // any change to the bill needs a fresh duplicate-guard token

  // p: { id, name, sku, price, mrp, gst, stock, track, promos }  (price and mrp in paise, gst in percent)
  function addItem(p) {
    const found = state.items.find((i) => i.id === p.id);
    if (found) {
      found.qty += 1;
      if (p.stock !== undefined) found.stock = p.stock;
      if (p.promos) found.promos = p.promos;
    } else {
      state.items.push({
        id: p.id, name: p.name, sku: p.sku || "",
        price: p.price, listPrice: p.price, mrp: p.mrp || p.price, gstBp: Math.round((p.gst || 0) * 100),
        qty: 1, stock: p.stock === undefined ? null : p.stock, track: !!p.track,
        dKind: null, dValue: 0, promos: p.promos || [],
      });
    }
    state.selected = p.id;
    touch();
    render();
  }

  function changeQty(id, delta) {
    const item = state.items.find((i) => i.id === id);
    if (!item) return;
    item.qty += delta;
    touch();
    if (item.qty < 1) removeItem(id);
    else render();
  }

  function removeItem(id) {
    state.items = state.items.filter((i) => i.id !== id);
    if (state.selected === id) state.selected = null;
    touch();
    render();
  }

  function resetSale() {
    state.items = [];
    state.selected = null;
    state.customer = null;
    state.bill = { kind: null, value: 0 };
    state.token = null;
    render();
  }

  // Asks the server which promotions are running right now for the items in the cart.
  async function refreshPromos() {
    if (!state.items.length) return true;
    try {
      const res = await fetch(PROMO_URL + "?ids=" + state.items.map((i) => i.id).join(","), { credentials: "same-origin" });
      if (!res.ok) throw new Error("bad status");
      const data = await res.json();
      state.items.forEach((it) => { it.promos = data.rules[String(it.id)] || []; });
      touch();
      render();
      return true;
    } catch (err) {
      showNote("Could not check the running promotions. Please try again.");
      return false;
    }
  }

  // ---------- Rendering ----------
  function render() {
    body.querySelectorAll(".cart-row").forEach((r) => r.remove());
    empty.style.display = state.items.length ? "none" : "flex";

    const calc = compute();

    calc.rows.forEach((r) => {
      const item = r.it;

      const row = el("div", "cart-row" + (item.id === state.selected ? " selected" : ""));
      row.addEventListener("click", () => { state.selected = item.id; render(); });

      const info = el("div", "item-info");
      const ph = el("div", "item-img");
      ph.innerHTML = '<i class="bi bi-image"></i>';
      info.appendChild(ph);

      const txt = el("div");
      txt.appendChild(el("div", "item-name", item.name));
      txt.appendChild(el("div", "item-sku", item.sku));
      if (r.promo > 0) txt.appendChild(el("div", "item-promo", "Promo: " + r.promoName));
      if (item.track && item.stock !== null && item.qty > item.stock) {
        txt.appendChild(el("div", "stock-warn", item.stock <= 0 ? "No stock in system" : "Only " + fmtQty(item.stock) + " in stock"));
      }
      info.appendChild(txt);
      row.appendChild(info);

      const qtyCell = el("div");
      const qb = el("div", "qty-box");
      const minus = el("button", "", "−");
      const plus = el("button", "", "+");
      minus.type = plus.type = "button";
      minus.addEventListener("click", (e) => { e.stopPropagation(); changeQty(item.id, -1); });
      plus.addEventListener("click", (e) => { e.stopPropagation(); changeQty(item.id, 1); });
      qb.append(minus, el("span", "", String(item.qty)), plus);
      qtyCell.appendChild(qb);
      row.appendChild(qtyCell);

      row.appendChild(el("div", "c-num", inr(item.mrp)));
      row.appendChild(el("div", "c-num", inr(item.price)));
      const mrpTotal = item.mrp * item.qty;
      const discountPercent = mrpTotal > 0 ? Math.max(0, (mrpTotal - r.total) / mrpTotal * 100) : 0;
      row.appendChild(el("div", "c-num", discountPercent.toFixed(2) + "%"));
      row.appendChild(el("div", "c-num", inr(r.total)));

      const del = el("button", "row-del");
      del.type = "button";
      del.innerHTML = '<i class="bi bi-trash"></i>';
      del.addEventListener("click", (e) => { e.stopPropagation(); removeItem(item.id); });
      row.appendChild(del);

      body.appendChild(row);
    });

    $("totItems").textContent = String(state.items.length);
    $("totSub").textContent = inr(calc.subtotal);
    $("totTax").textContent = inr(calc.tax);
    $("totGrand").textContent = inr(calc.total);
    $("totDisc").textContent = calc.discount > 0 ? "after discount of " + inr(calc.discount) : "";

    $("metaCustomer").textContent = state.customer ? state.customer.name : "Walk-in customer";
    $("metaClear").hidden = !state.customer;
  }

  // ---------- Dialogs ----------
  const backdrop = $("dlgBackdrop");
  const dialogs = ["dlgPay", "dlgDisc", "dlgCust", "dlgDone", "dlgPrice", "dlgHeld", "dlgDrawer", "dlgRefund", "dlgPrint"].map($);

  function openDialog(id) {
    dialogs.forEach((d) => (d.hidden = d.id !== id));
    backdrop.hidden = false;
  }

  function hideDialogs() {
    backdrop.hidden = true;
    dialogs.forEach((d) => (d.hidden = true));
  }

  function closeDialogs() {
    hideDialogs();
    searchInput.focus();
  }

  document.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", closeDialogs));
  backdrop.addEventListener("mousedown", (e) => {
    if (e.target === backdrop && $("dlgDone").hidden) closeDialogs();
  });

  function requireItems() {
    if (!state.items.length) { showNote("Add items to the bill first."); return false; }
    return true;
  }

  // ---------- Payment ----------
  const payInputs = { cash: $("payCash"), upi: $("payUpi"), card: $("payCard") };
  let payCalc = { total: 0, valid: false };

  async function openPayment(preset) {
    if (!requireItems()) return;
    if (!(await refreshPromos())) return;      // make sure the promotions are the ones running right now
    const total = compute().total;
    Object.values(payInputs).forEach((i) => (i.value = ""));
    $("payUpiRef").value = "";
    $("payCardRef").value = "";
    payInputs[preset].value = hStr(total);
    $("payError").hidden = true;
    $("payTotal").textContent = inr(total);
    openDialog("dlgPay");
    updatePay();
    payInputs[preset].focus();
    payInputs[preset].select();
  }

  function readPay() {
    const vals = {};
    for (const key of Object.keys(payInputs)) {
      const v = toHundredths(payInputs[key].value);
      if (v === null) return null;
      vals[key] = v;
    }
    return vals;
  }

  function updatePay() {
    const total = compute().total;
    const vals = readPay();
    const errBox = $("payError");
    let message = "";
    let valid = false;

    if (!vals) {
      message = "Enter amounts like 250 or 250.50.";
      $("payPaid").textContent = "—";
    } else {
      const paid = vals.cash + vals.upi + vals.card;
      const change = paid - total;
      $("payPaid").textContent = inr(paid);
      if (paid < total) {
        $("payLeftLabel").textContent = "Due";
        $("payLeft").textContent = inr(total - paid);
      } else {
        $("payLeftLabel").textContent = "Change to return";
        $("payLeft").textContent = inr(change);
      }
      if (change > vals.cash) message = "Only cash can be overpaid. UPI and card cannot be more than the bill.";
      else valid = paid >= total && total > 0;
    }

    if (!state.busy) {
      errBox.hidden = !message;
      errBox.textContent = message;
    }
    payCalc = { total, valid };
    $("payConfirm").disabled = !valid || state.busy;
  }

  Object.values(payInputs).forEach((i) => i.addEventListener("input", updatePay));

  document.querySelectorAll("[data-fill]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const vals = readPay() || { cash: 0, upi: 0, card: 0 };
      const target = btn.dataset.fill;
      const key = target === "payCash" ? "cash" : target === "payUpi" ? "upi" : "card";
      const others = Object.keys(vals).filter((k) => k !== key).reduce((s, k) => s + vals[k], 0);
      $(target).value = hStr(Math.max(compute().total - others, 0));
      updatePay();
    });
  });

  function buildPayload(vals) {
    const calc = compute();
    const payments = [];
    if (vals.cash > 0) payments.push({ method: "cash", amount: hStr(vals.cash), reference: "" });
    if (vals.upi > 0) payments.push({ method: "upi", amount: hStr(vals.upi), reference: $("payUpiRef").value.trim() });
    if (vals.card > 0) payments.push({ method: "card", amount: hStr(vals.card), reference: $("payCardRef").value.trim() });

    if (!state.token) state.token = makeToken();   // reused if the same bill is retried
    return {
      client_token: state.token,
      customer_id: state.customer ? state.customer.id : null,
      items: state.items.map((it) => ({
        product_id: it.id,
        quantity: String(it.qty),
        price: it.price !== it.listPrice ? hStr(it.price) : null,
        discount: { type: it.dKind || "none", value: it.dKind ? hStr(it.dValue) : "0" },
      })),
      bill_discount: { type: state.bill.kind || "none", value: state.bill.kind ? hStr(state.bill.value) : "0" },
      payments,
      expected_total: calc.total,
    };
  }

  async function completeSale() {
    if (state.busy || !payCalc.valid) return;
    const vals = readPay();
    if (!vals) return;

    state.busy = true;
    $("payConfirm").disabled = true;
    $("payConfirm").textContent = "Saving...";
    $("payError").hidden = true;

    try {
      const res = await fetch(CHECKOUT_URL, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRFToken": CSRF },
        body: JSON.stringify(buildPayload(vals)),
      });
      let data = {};
      try { data = await res.json(); } catch (e) { /* not JSON */ }

      if (res.ok && data.ok) {
        showDone(data);
      } else {
        $("payError").textContent = data.error || "The bill could not be saved. Please try again.";
        $("payError").hidden = false;
        if (res.status === 409) {                     // a price or promotion changed: refresh and show the new total
          await refreshPromos();
          $("payTotal").textContent = inr(compute().total);
        }
      }
    } catch (err) {
      $("payError").textContent =
        "Could not reach the server. The bill may or may not have saved. Press Complete sale again. It will not create a second bill.";
      $("payError").hidden = false;
    } finally {
      state.busy = false;
      $("payConfirm").textContent = "Complete sale";
      updatePay();
    }
  }

  $("payConfirm").addEventListener("click", completeSale);
  $("dlgPay").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); completeSale(); }
  });

  // ---------- After the sale ----------
  function startNewSale() {
    resetSale();
    searchInput.value = "";
    searchInput.focus();
  }

  function showDone(data) {
    // The full-screen Actions page (receipt, invoice, WhatsApp, PDF, notes) lives in pos-done.js
    if (window.PosDone) {
      hideDialogs();
      window.PosDone.open(data, startNewSale);
      return;
    }

    // Fallback if pos-done.js is not loaded: the simple result window
    $("doneInvoice").textContent = data.invoice_no;
    $("doneTotal").textContent = "₹ " + data.total;
    $("doneChange").textContent = "₹ " + data.change;
    const warn = $("doneWarn");
    warn.hidden = !(data.warnings && data.warnings.length);
    warn.textContent = (data.warnings || []).join(" ");
    openDialog("dlgDone");
    $("doneNew").focus();
  }

  $("doneNew").addEventListener("click", () => {
    startNewSale();
    closeDialogs();
  });

  // ---------- Discount ----------
  function openDiscount() {
    if (!requireItems()) return;
    const scope = $("discScope");
    scope.innerHTML = "";
    scope.appendChild(new Option("Whole bill", "bill"));
    const sel = state.items.find((i) => i.id === state.selected);
    if (sel) scope.appendChild(new Option("Selected item: " + sel.name, "item"));
    scope.value = sel ? "item" : "bill";
    $("discValue").value = "";
    $("discError").hidden = true;
    $("discHint").textContent = MAX_DISCOUNT ? "Your limit is " + MAX_DISCOUNT + "% of the bill (promotions do not count)." : "";
    openDialog("dlgDisc");
    $("discValue").focus();
  }

  $("discApply").addEventListener("click", () => {
    const kind = $("discType").value;
    const value = toHundredths($("discValue").value);
    const err = $("discError");
    if (value === null || value <= 0) { err.textContent = "Enter a discount above zero, like 10 or 25.50."; err.hidden = false; return; }
    if (kind === "percent" && value > 10000) { err.textContent = "A percentage cannot be above 100."; err.hidden = false; return; }

    if ($("discScope").value === "item") {
      const it = state.items.find((i) => i.id === state.selected);
      if (it) { it.dKind = kind; it.dValue = value; }
    } else {
      state.bill = { kind, value };
    }
    touch();
    render();
    closeDialogs();
  });

  $("discRemove").addEventListener("click", () => {
    if ($("discScope").value === "item") {
      const it = state.items.find((i) => i.id === state.selected);
      if (it) { it.dKind = null; it.dValue = 0; }
    } else {
      state.bill = { kind: null, value: 0 };
    }
    touch();
    render();
    closeDialogs();
  });

  $("dlgDisc").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); $("discApply").click(); }
  });

  // ---------- Customer ----------
  let custTimer = null;
  let custReq = 0;

  function renderCustomers(list) {
    const box = $("custList");
    box.innerHTML = "";

    const walk = el("div", "cust-item");
    walk.append(el("span", "", "Walk-in customer"), el("small", "", "default"));
    walk.addEventListener("click", () => { state.customer = null; touch(); render(); closeDialogs(); });
    box.appendChild(walk);

    list.forEach((c) => {
      const row = el("div", "cust-item");
      row.append(el("span", "", c.name), el("small", "", c.phone || ""));
      row.addEventListener("click", () => { state.customer = { id: c.id, name: c.name }; touch(); render(); closeDialogs(); });
      box.appendChild(row);
    });
    if (!list.length) box.appendChild(el("div", "cust-empty", "No other customers found."));
  }

  async function loadCustomers(q) {
    const id = ++custReq;
    try {
      const res = await fetch(CUSTOMER_URL + "?q=" + encodeURIComponent(q), { credentials: "same-origin" });
      if (!res.ok) throw new Error("bad status");
      const data = await res.json();
      if (id === custReq) renderCustomers(data.results);
    } catch (err) {
      if (id === custReq) { $("custList").innerHTML = ""; $("custList").appendChild(el("div", "cust-empty", "Could not load customers.")); }
    }
  }

  function openCustomer() {
    $("custSearch").value = "";
    openDialog("dlgCust");
    loadCustomers("");
    $("custSearch").focus();
  }

  $("custSearch").addEventListener("input", () => {
    clearTimeout(custTimer);
    custTimer = setTimeout(() => loadCustomers($("custSearch").value.trim()), 200);
  });

  $("metaClear").addEventListener("click", () => { state.customer = null; touch(); render(); });

  // ---------- Product search ----------
  let hits = [];
  let active = 0;
  let lastQuery = null;
  let timer = null;
  let reqId = 0;

  function closeResults() {
    resultsBox.hidden = true;
    resultsBox.innerHTML = "";
  }

  function renderResults() {
    resultsBox.innerHTML = "";
    if (!hits.length) {
      resultsBox.appendChild(el("div", "pos-results-empty", "No product found"));
      resultsBox.hidden = false;
      return;
    }
    hits.forEach((h, i) => {
      const row = el("div", "pos-result" + (i === active ? " active" : ""));

      const left = el("div");
      left.appendChild(el("div", "pr-name", h.name));
      left.appendChild(el("div", "pr-meta", [h.code, h.barcode].filter(Boolean).join("  ·  ")));
      if (h.promos && h.promos.length) {
        left.appendChild(el("div", "pr-promo", "Promo: " + h.promos.slice(0, 2).map(promoLabel).join(" · ")));
      }

      const right = el("div", "pr-right");
      right.appendChild(el("div", "pr-price", inr(h.price)));
      if (h.track) {
        const out = h.stock <= 0;
        right.appendChild(el("div", "pr-stock " + (out ? "out" : "ok"), out ? "No stock" : fmtQty(h.stock) + " in stock"));
      }

      row.append(left, right);
      row.addEventListener("mousedown", (e) => { e.preventDefault(); pick(h); });
      resultsBox.appendChild(row);
    });
    resultsBox.hidden = false;
  }

  async function runSearch(q) {
    const id = ++reqId;
    try {
      const res = await fetch(SEARCH_URL + "?q=" + encodeURIComponent(q), { credentials: "same-origin" });
      if (!res.ok) throw new Error("bad status");
      const data = await res.json();
      if (id !== reqId) return;
      hits = data.results;
      active = 0;
      lastQuery = q;
      renderResults();
    } catch (err) {
      if (id !== reqId) return;
      showNote("Search failed. Check that the server is running and try again.");
      closeResults();
    }
  }

  function pick(h) {
    addItem({ id: h.id, name: h.name, sku: h.code, price: h.price, mrp: h.mrp, gst: h.gst, stock: h.stock, track: h.track, promos: h.promos });
    if (h.track && h.stock <= 0) {
      showNote("Warning: " + h.name + " shows no stock in the system. The sale is still allowed.");
    } else {
      showNote("");
    }
    searchInput.value = "";
    lastQuery = null;
    hits = [];
    closeResults();
    searchInput.focus();
  }

  searchInput.addEventListener("input", () => {
    clearTimeout(timer);
    const q = searchInput.value.trim();
    if (q.length < 2) { lastQuery = null; closeResults(); return; }
    timer = setTimeout(() => runSearch(q), 200);
  });

  searchInput.addEventListener("keydown", async (e) => {
    if (e.key === "ArrowDown" && hits.length) {
      e.preventDefault();
      active = (active + 1) % hits.length;
      renderResults();
    } else if (e.key === "ArrowUp" && hits.length) {
      e.preventDefault();
      active = (active - 1 + hits.length) % hits.length;
      renderResults();
    } else if (e.key === "Escape") {
      closeResults();
    } else if (e.key === "Enter") {
      e.preventDefault();
      const q = searchInput.value.trim();
      if (!q) return;
      clearTimeout(timer);
      if (lastQuery !== q) await runSearch(q);
      if (searchInput.value.trim() !== q) return;
      if (!hits.length) { showNote('No product found for "' + q + '".'); return; }
      pick(hits.find((h) => h.exact) || hits[active]);
    }
  });

  searchInput.addEventListener("blur", () => setTimeout(closeResults, 150));

  // ---------- Actions ----------
  function focusSearch() { searchInput.focus(); searchInput.select(); }

  function focusQuantity() {
    if (!state.items.length) return;
    state.selected = state.selected || state.items[state.items.length - 1].id;
    render();
  }

  function newSale() {
    if (state.items.length && !confirm("Start a new sale? The current bill will be cleared.")) return;
    resetSale();
    searchInput.value = "";
    closeResults();
    showNote("");
    focusSearch();
  }

  function voidOrder() {
    if (!state.items.length) return;
    if (confirm("Void this order? All items will be removed.")) resetSale();
  }

  // ---------- Side menu ----------
  const panel = $("sidePanel");
  const sideBackdrop = $("sideBackdrop");

  function openMenu() {
    panel.classList.add("open");
    sideBackdrop.classList.add("open");
    panel.setAttribute("aria-hidden", "false");
  }

  function closeMenu() {
    panel.classList.remove("open");
    sideBackdrop.classList.remove("open");
    panel.setAttribute("aria-hidden", "true");
  }

  $("sideClose").addEventListener("click", closeMenu);
  sideBackdrop.addEventListener("click", closeMenu);

  $("userInfoBtn").addEventListener("click", () => {
    const box = $("userInfoBox");
    box.hidden = !box.hidden;
  });

  $("fsBtn").addEventListener("click", () => {
    if (!document.fullscreenElement) document.documentElement.requestFullscreen();
    else document.exitFullscreen();
  });

  // ---------- Button wiring ----------
  const actions = {
    search: focusSearch, quantity: focusQuantity, new: newSale, void: voidOrder, menu: openMenu,
    cash: () => openPayment("cash"), card: () => openPayment("card"), pay: () => openPayment("cash"),
    discount: openDiscount, customer: openCustomer,
  };

  document.querySelectorAll("[data-action]").forEach((btn) => {
    btn.addEventListener("click", () => actions[btn.dataset.action]());
  });

  // Keyboard shortcuts
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      if (!backdrop.hidden && $("dlgDone").hidden) closeDialogs();
      closeMenu();
      return;
    }
    if (!backdrop.hidden) return;   // a dialog is open: leave the keys to it
    const map = { F2: openDiscount, F3: focusSearch, F4: focusQuantity, F8: newSale, F10: () => openPayment("cash") };
    if (map[e.key]) { e.preventDefault(); map[e.key](); return; }
    if ((e.key === "+" || e.key === "-") && document.activeElement !== searchInput && state.selected) {
      e.preventDefault();
      changeQty(state.selected, e.key === "+" ? 1 : -1);
    }
  });

  // Clock
  function tick() {
    const d = new Date();
    $("posDate").textContent = d.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
    $("posTime").textContent = d.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", hour12: true });
  }
  tick();
  setInterval(tick, 30000);

  window.POS = {
    app, state, addItem, resetSale, render, touch, showNote, openDialog, closeDialogs, hideDialogs,
    requireItems, inr, hStr, toHundredths, el, compute, CSRF,
  };
  render();
  searchInput.focus();
})();