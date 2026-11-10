(function () {
  "use strict";
  const P = window.POS;
  if (!P) { console.error("pos-extras.js needs the updated pos.js (window.POS is missing)."); return; }

  const { state, render, touch, showNote, openDialog, closeDialogs, hideDialogs, requireItems, inr, hStr, toHundredths, el } = P;
  const $ = (id) => document.getElementById(id);
  const D = P.app.dataset;
  const IS_ADMIN = D.admin === "1";
  const money = (s) => "₹ " + Number(s).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

  async function post(url, body) {
    try {
      const res = await fetch(url, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRFToken": P.CSRF },
        body: JSON.stringify(body || {}),
      });
      let data = {};
      try { data = await res.json(); } catch (e) { /* not JSON */ }
      return { ok: res.ok && data.ok !== false, status: res.status, data };
    } catch (e) {
      return { ok: false, status: 0, data: { error: "Could not reach the server." } };
    }
  }

  function showErr(id, text) { const b = $(id); b.textContent = text; b.hidden = !text; }

  // ---------- Price ----------
  function selectedItem() { return state.items.find((i) => i.id === state.selected); }

  function openPrice() {
    if (!requireItems()) return;
    const it = selectedItem();
    if (!it) { showNote("Click an item in the bill first, then press Price."); return; }
    $("priceItem").textContent = it.name + " · list price " + inr(it.listPrice);
    $("priceValue").value = hStr(it.price);
    $("priceReset").disabled = it.price === it.listPrice;
    $("priceHint").textContent = "Enter the agreed price per unit. Standard discounts remain subject to the cashier limit.";
    showErr("priceError", "");
    openDialog("dlgPrice");
    $("priceValue").focus();
    $("priceValue").select();
  }

  $("priceApply").addEventListener("click", () => {
    const it = selectedItem();
    if (!it) { closeDialogs(); return; }
    const v = toHundredths($("priceValue").value);
    if (v === null || v <= 0) { showErr("priceError", "Enter a price above zero, like 250 or 250.50."); return; }
    it.price = v;
    touch(); render(); closeDialogs();
  });

  $("priceReset").addEventListener("click", () => {
    const it = selectedItem();
    if (it) { it.price = it.listPrice; touch(); render(); }
    closeDialogs();
  });

  $("dlgPrice").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); $("priceApply").click(); }
  });

  // ---------- Save sale / saved sales ----------
  async function holdSale() {
    if (!state.items.length) { openHeld(); return; }
    const r = await post(D.heldSaveUrl, {
      customer_id: state.customer ? state.customer.id : null,
      bill: { kind: state.bill.kind, value: state.bill.value },
      total: P.compute().total,
      items: state.items.map((i) => ({
        id: i.id, qty: String(i.qty), dKind: i.dKind, dValue: i.dValue,
        price: i.price !== i.listPrice ? i.price : null,
      })),
    });
    if (!r.ok) { showNote(r.data.error || "Could not save the sale."); return; }
    P.resetSale();
    $("posSearch").value = "";
    $("posSearch").focus();
    showNote("Sale saved as " + r.data.label + ". Open it from More > View open sales, or press F9 on an empty bill.");
  }

  async function openHeld() {
    showErr("heldMsg", "");
    const box = $("heldList");
    box.innerHTML = "";
    box.appendChild(el("div", "cust-empty", "Loading..."));
    openDialog("dlgHeld");
    try {
      const res = await fetch(D.heldUrl, { credentials: "same-origin" });
      if (!res.ok) throw new Error("bad status");
      const data = await res.json();
      box.innerHTML = "";
      if (!data.results.length) { box.appendChild(el("div", "cust-empty", "No saved sales.")); return; }
      data.results.forEach((h) => {
        const row = el("div", "held-item");
        const left = el("div");
        left.appendChild(el("strong", "", "#" + h.id + " · " + h.customer));
        left.appendChild(el("small", "", h.items + " item(s) · " + money(h.total) + " · " + h.by + " · " + h.at));

        const right = el("div", "d-flex gap-1");
        const open = el("button", "btn btn-sm btn-brand", "Resume");
        open.type = "button";
        open.addEventListener("click", () => resume(h.id));
        const del = el("button", "btn btn-sm btn-outline-danger");
        del.type = "button";
        del.title = "Delete";
        del.innerHTML = '<i class="bi bi-trash"></i>';
        del.addEventListener("click", async () => {
          if (!confirm("Delete saved sale #" + h.id + "?")) return;
          const r = await post(D.heldDeleteUrl.replace("/0/", "/" + h.id + "/"));
          if (r.ok) openHeld(); else showErr("heldMsg", r.data.error || "Could not delete.");
        });
        right.append(open, del);
        row.append(left, right);
        box.appendChild(row);
      });
    } catch (e) {
      box.innerHTML = "";
      box.appendChild(el("div", "cust-empty", "Could not load saved sales."));
    }
  }

  async function resume(id) {
    if (state.items.length) { showErr("heldMsg", "Save or clear the current bill first."); return; }
    const r = await post(D.heldResumeUrl.replace("/0/", "/" + id + "/"));
    if (!r.ok) { showErr("heldMsg", r.data.error || "Could not open the saved sale."); return; }
    const d = r.data;
    d.items.forEach((x) => {
      const p = x.product;
      state.items.push({
        id: p.id, name: p.name, sku: p.code || "",
        price: x.price != null ? x.price : p.price, listPrice: p.price,
        mrp: p.mrp || p.price, gstBp: Math.round((p.gst || 0) * 100),
        qty: Number(x.qty), stock: p.stock === undefined ? null : p.stock, track: !!p.track,
        dKind: x.dKind || null, dValue: x.dValue || 0,
      });
    });
    state.bill = d.bill && d.bill.kind ? { kind: d.bill.kind, value: d.bill.value } : { kind: null, value: 0 };
    state.customer = d.customer || null;
    state.selected = state.items.length ? state.items[state.items.length - 1].id : null;
    touch(); render(); closeDialogs();
    if (d.skipped && d.skipped.length) showNote("Not added (inactive or deleted): " + d.skipped.join(", "));
  }

  // ---------- Cash drawer ----------
  // Browsers cannot open a drawer by themselves. This works for printers that show up as a serial
  // port (Chrome/Edge). Printers that open the drawer through their driver need no code.
  async function kickDrawer() {
    if (!("serial" in navigator)) return false;
    try {
      let port = (await navigator.serial.getPorts())[0];
      if (!port) port = await navigator.serial.requestPort();      // asked once, then remembered
      await port.open({ baudRate: 9600 });
      const w = port.writable.getWriter();
      await w.write(new Uint8Array([0x1B, 0x70, 0x00, 0x19, 0xFA]));   // ESC p 0 : pulse the drawer
      w.releaseLock();
      await port.close();
      return true;
    } catch (e) { return false; }
  }

  function openDrawer() { showErr("drawerError", ""); openDialog("dlgDrawer"); $("drawerGo").focus(); }

  $("drawerGo").addEventListener("click", async () => {
    $("drawerGo").disabled = true;
    const r = await post(D.drawerUrl, { reason: $("drawerReason").value });
    if (!r.ok) { $("drawerGo").disabled = false; showErr("drawerError", r.data.error || "Could not log this."); return; }
    const opened = await kickDrawer();
    $("drawerGo").disabled = false;
    closeDialogs();
    showNote(opened ? "Cash drawer opened." : "Logged. No drawer is connected to this browser, so it did not open.");
  });

  // ---------- Refund ----------
  function openRefund() {
    if (!IS_ADMIN) { showNote("Only an admin can process refunds. Ask an admin to sign in."); return; }
    $("refundNo").value = "";
    showErr("refundError", "");
    openDialog("dlgRefund");
    $("refundNo").focus();
  }

  async function findBill() {
    const q = $("refundNo").value.trim();
    if (!q) return;
    try {
      const res = await fetch(D.findUrl + "?q=" + encodeURIComponent(q), { credentials: "same-origin" });
      let data = {};
      try { data = await res.json(); } catch (e) { /* not JSON */ }
      if (!res.ok) { showErr("refundError", data.error || "Could not find that bill."); return; }
      window.open(data.url, "_blank");
      closeDialogs();
      showNote("Opened the return page for " + data.invoice_no + " in a new tab.");
    } catch (e) {
      showErr("refundError", "Could not reach the server.");
    }
  }
  $("refundGo").addEventListener("click", findBill);
  $("dlgRefund").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); findBill(); } });

  // ---------- Lock ----------
  function setLocked(on) {
    document.body.classList.toggle("pos-locked", on);
    $("lockScreen").hidden = !on;
    if (on) {
      $("lockPass").value = "";
      showErr("lockError", "");
      setTimeout(() => $("lockPass").focus(), 50);
    } else {
      $("posSearch").focus();
    }
  }

  async function lockNow() {
    const r = await post(D.lockUrl);
    if (!r.ok) { showNote("Could not lock the POS."); return; }
    hideDialogs();
    $("sideClose").click();
    setLocked(true);
  }

  async function unlock() {
    const pw = $("lockPass").value;
    if (!pw) return;
    $("lockGo").disabled = true;
    const r = await post(D.unlockUrl, { password: pw });
    $("lockGo").disabled = false;
    if (r.ok) { setLocked(false); return; }
    showErr("lockError", r.data.error || "Could not unlock.");
    $("lockPass").select();
  }
  $("lockGo").addEventListener("click", unlock);
  $("lockPass").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); unlock(); } });

  if (!$("lockScreen").hidden) setLocked(true);   // the server says this session is locked
  // ---------- Print settings ----------
  const PS = JSON.parse($("printDialogData").textContent);   // null for cashiers

  const SWITCHES = {
    psRecSwitches: [["r_tax_totals", "Print GST totals"], ["r_item_gst", "Print GST rate on each item"],
      ["r_unit", "Print measurement unit"], ["r_short_no", "Short receipt number"],
      ["r_order_no", "Print order number (today's bill count)"]],
    psRecCust: [["r_cust_name", "Name"], ["r_cust_code", "Code"], ["r_cust_gstin", "Tax number (GSTIN)"],
      ["r_cust_address", "Address"]],
    psInvCols: [["i_col_tax", "Tax columns"], ["i_col_discount", "Discount column"]],
    psInvCust: [["i_cust_gstin", "Tax number (GSTIN)"], ["i_cust_code", "Code"], ["i_cust_phone", "Phone number"],
      ["i_cust_email", "Email"]],
    psInvOther: [["i_a5", "Print in A5 size"], ["i_payments", "Payment methods"]],
  };

  function buildPrintDialog() {
    Object.keys(SWITCHES).forEach((boxId) => {
      SWITCHES[boxId].forEach(([name, text]) => {
        const row = el("div", "form-check form-switch");
        const input = el("input", "form-check-input");
        input.type = "checkbox";
        input.id = "ps_" + name;
        const label = el("label", "form-check-label", text);
        label.htmlFor = input.id;
        row.append(input, label);
        $(boxId).appendChild(row);
      });
    });
    Object.keys(PS.defaults).forEach((key) => {
      const wrap = el("div");
      const label = el("label", "dlg-label", PS.defaults[key]);
      label.htmlFor = "ps_lbl_" + key;
      const input = el("input", "form-control form-control-sm");
      input.id = "ps_lbl_" + key;
      input.maxLength = 30;
      input.placeholder = PS.defaults[key];
      wrap.append(label, input);
      $("psLabels").appendChild(wrap);
    });
    document.querySelectorAll(".ps-tab").forEach((tab) => {
      tab.addEventListener("click", () => {
        document.querySelectorAll(".ps-tab").forEach((t) => t.classList.toggle("active", t === tab));
        document.querySelectorAll(".ps-panel").forEach((p) => { p.hidden = p.id !== tab.dataset.tab; });
      });
    });
    $("psSave").addEventListener("click", savePrint);
  }

  function fillPrint() {
    Object.keys(PS.settings).forEach((k) => {
      if (k === "labels") return;
      const e = $("ps_" + k);
      if (!e) return;
      if (e.type === "checkbox") e.checked = !!PS.settings[k]; else e.value = PS.settings[k];
    });
    Object.keys(PS.defaults).forEach((k) => { $("ps_lbl_" + k).value = (PS.settings.labels && PS.settings.labels[k]) || ""; });
  }

  function readPrint() {
    const out = {};
    Object.keys(PS.settings).forEach((k) => {
      if (k === "labels") return;
      const e = $("ps_" + k);
      if (!e) return;
      if (e.type === "checkbox") out[k] = e.checked;
      else if (typeof PS.settings[k] === "number") out[k] = Number(e.value);
      else out[k] = e.value;
    });
    out.labels = {};
    Object.keys(PS.defaults).forEach((k) => { out.labels[k] = $("ps_lbl_" + k).value.trim(); });
    return out;
  }

  function openPrint() {
    if (!PS) { showNote("Only an admin can change print settings."); return; }
    fillPrint();
    showErr("psError", "");
    openDialog("dlgPrint");
  }

  async function savePrint() {
    $("psSave").disabled = true;
    const r = await post(D.printUrl, readPrint());
    $("psSave").disabled = false;
    if (!r.ok) { showErr("psError", r.data.error || "Could not save the print settings."); return; }
    PS.settings = r.data.settings;
    closeDialogs();
    showNote("Print settings saved. They apply the next time you open a receipt or invoice.");
  }

  if (PS) buildPrintDialog();
  $("printSettingsBtn").addEventListener("click", () => { $("sideClose").click(); openPrint(); });
  // ---------- Wiring ----------
  const wire = { price: openPrice, hold: holdSale, drawer: openDrawer, refund: openRefund, lock: lockNow, "held-list": openHeld };
  document.querySelectorAll("[data-x]").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (btn.dataset.x === "held-list") $("sideClose").click();
      wire[btn.dataset.x]();
    });
  });

  // Runs before pos.js's own key handler. While locked it swallows every key outside the lock box.
  document.addEventListener("keydown", (e) => {
    if (document.body.classList.contains("pos-locked")) {
      if (!(e.target.closest && e.target.closest("#lockScreen"))) e.stopImmediatePropagation();
      return;
    }
    if (e.key === "F9" && $("dlgBackdrop").hidden) { e.preventDefault(); holdSale(); }
  }, true);
})();