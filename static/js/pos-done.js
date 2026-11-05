(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const screenEl = $("doneScreen");
  if (!screenEl) return;

  const app = document.querySelector(".pos-app");
  const CSRF = app.dataset.csrf;
  const STORE = screenEl.dataset.store || "Artyflakes";
  const FOOTER = screenEl.dataset.footer || "";

  let sale = null;
  let onDone = null;

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }

  // The templates contain id 0 as a placeholder, e.g. /receipt/0/
  const urlFor = (tpl, id) => tpl.replace("/0/", "/" + id + "/");

  function message(text) {
    const box = $("dsMsg");
    box.textContent = text;
    box.hidden = !text;
  }

  // ---------- Open / close ----------
  function open(data, done) {
    sale = data;
    onDone = done;

    const list = $("dsItems");
    list.innerHTML = "";
    (data.lines || []).forEach((l) => {
      const row = el("div", "ds-item");
      const left = el("div");
      left.append(el("div", "ds-name", l.name), el("div", "ds-qty", l.qty + " x " + l.price));
      row.append(left, el("div", "ds-amt", l.amount));
      list.appendChild(row);
    });

    $("dsSub").textContent = data.subtotal;
    $("dsDisc").textContent = data.discount;
    $("dsDiscRow").hidden = data.discount === "0.00";
    $("dsTax").textContent = data.tax;
    $("dsTaxRow").hidden = data.tax === "0.00";
    $("dsTotal").textContent = data.total;
    $("dsPaid").textContent = data.paid;
    $("dsInvoice").textContent = data.invoice_no;
    $("dsChange").textContent = "₹ " + data.change;

    const warn = $("dsWarn");
    warn.hidden = !(data.warnings && data.warnings.length);
    warn.textContent = (data.warnings || []).join(" ");

    $("dsWa").hidden = true;
    $("dsNote").hidden = true;
    $("waError").hidden = true;
    $("waPhone").value = data.customer_phone || "";
    $("noteText").value = "";
    message("");

    screenEl.hidden = false;
    $("dsReceipt").focus();
  }

  function finish() {
    screenEl.hidden = true;
    if (onDone) onDone();
  }

  // ---------- Printing ----------
  function printDoc(kind) {
    const w = window.open(urlFor(screenEl.dataset[kind + "Url"], sale.sale_id) + "?print=1", "_blank");
    if (!w) message("The print window was blocked. Allow pop-ups for this site and try again.");
    else if (kind === "invoice" && screenEl.dataset.pdf === "1") message("");
    return w;
  }

  // ---------- WhatsApp ----------
  // Indian mobile numbers only: 10 digits starting 6-9, with an optional 0 or 91 in front.
  function waNumber(raw) {
    let d = String(raw).replace(/\D/g, "");
    if (d.length === 11 && d[0] === "0") d = d.slice(1);
    if (d.length === 10) d = "91" + d;
    return /^91[6-9]\d{9}$/.test(d) ? d : null;
  }

  function waText() {
    const out = ["*" + STORE + "*", "Invoice: " + sale.invoice_no, "Date: " + sale.created, ""];
    (sale.lines || []).forEach((l) => out.push(l.name + " x " + l.qty + " = ₹" + l.amount));
    out.push("");
    if (sale.discount !== "0.00") out.push("Discount: ₹" + sale.discount);
    out.push("*Total: ₹" + sale.total + "*");
    if (sale.tax !== "0.00") out.push("(includes GST ₹" + sale.tax + ")");
    if (FOOTER) { out.push(""); out.push(FOOTER); }
    return out.join("\n");
  }

  function openWhatsApp() {
    const number = waNumber($("waPhone").value);
    const err = $("waError");
    if (!number) {
      err.textContent = "Enter a valid 10-digit Indian mobile number.";
      err.hidden = false;
      return;
    }
    err.hidden = true;
    window.open("https://wa.me/" + number + "?text=" + encodeURIComponent(waText()), "_blank");
  }

  // ---------- Note ----------
  async function saveNote() {
    const note = $("noteText").value.trim().slice(0, 255);
    try {
      const res = await fetch(urlFor(screenEl.dataset.noteUrl, sale.sale_id), {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRFToken": CSRF },
        body: JSON.stringify({ note }),
      });
      if (!res.ok) throw new Error("bad status");
      message(note ? "Note saved. It will show on the receipt and invoice." : "Note removed.");
    } catch (err) {
      message("The note could not be saved. Please try again.");
    }
  }

  // ---------- Wiring ----------
  const doActions = {
    receipt: () => { message(""); printDoc("receipt"); },
    invoice: () => { message(""); printDoc("invoice"); },
    pdf: () => { printDoc("invoice"); message("In the print window, choose 'Save as PDF' as the printer."); },
    whatsapp: () => { $("dsNote").hidden = true; $("dsWa").hidden = false; message(""); $("waPhone").focus(); },
    note: () => { $("dsWa").hidden = true; $("dsNote").hidden = false; message(""); $("noteText").focus(); },
  };

  document.querySelectorAll("[data-do]").forEach((btn) => {
    btn.addEventListener("click", () => doActions[btn.dataset.do]());
  });

  $("waOpen").addEventListener("click", openWhatsApp);
  $("noteSave").addEventListener("click", saveNote);
  $("dsDone").addEventListener("click", finish);

  // While this screen is open, keep the POS keyboard shortcuts (F2, F3, F8 ...) quiet.
  document.addEventListener("keydown", (e) => {
    if (screenEl.hidden) return;
    e.stopPropagation();
    if (e.key === "Enter" && e.target === $("waPhone")) { e.preventDefault(); openWhatsApp(); }
    else if (e.key === "Enter" && e.target === $("noteText")) { e.preventDefault(); saveNote(); }
  }, true);

  window.PosDone = { open };
})();