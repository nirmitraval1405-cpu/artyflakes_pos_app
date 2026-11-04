(function () {
  "use strict";

  // All money is stored in paise (integers) to avoid floating-point errors.
  const state = { items: [], selected: null };

  const $ = (id) => document.getElementById(id);
  const body = $("cartBody");
  const empty = $("cartEmpty");
  const searchInput = $("posSearch");

  const inr = (paise) =>
    "₹ " + (paise / 100).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }

  // ---------- Cart logic ----------
  // Phase 2 will call this with a real product from the server:
  // POS.addItem({ id, name, sku, image, price, mrp })   (price and mrp in paise)
  function addItem(p) {
    const found = state.items.find((i) => i.id === p.id);
    if (found) found.qty += 1;
    else state.items.push({ id: p.id, name: p.name, sku: p.sku || "", image: p.image || "", price: p.price, mrp: p.mrp || p.price, qty: 1 });
    state.selected = p.id;
    render();
  }

  function changeQty(id, delta) {
    const item = state.items.find((i) => i.id === id);
    if (!item) return;
    item.qty += delta;
    if (item.qty < 1) removeItem(id);
    else render();
  }

  function removeItem(id) {
    state.items = state.items.filter((i) => i.id !== id);
    if (state.selected === id) state.selected = null;
    render();
  }

  function clearCart() {
    state.items = [];
    state.selected = null;
    render();
  }

  // ---------- Rendering ----------
  function render() {
    body.querySelectorAll(".cart-row").forEach((r) => r.remove());
    empty.style.display = state.items.length ? "none" : "flex";

    let units = 0, subtotal = 0;

    state.items.forEach((item) => {
      const saving = Math.max(item.mrp - item.price, 0);
      const amount = item.price * item.qty;
      units += item.qty;
      subtotal += amount;

      const row = el("div", "cart-row" + (item.id === state.selected ? " selected" : ""));
      row.addEventListener("click", () => { state.selected = item.id; render(); });

      const info = el("div", "item-info");
      if (item.image) {
        const img = el("img", "item-img");
        img.src = item.image;
        img.alt = "";
        info.appendChild(img);
      } else {
        const ph = el("div", "item-img");
        ph.innerHTML = '<i class="bi bi-image"></i>';
        info.appendChild(ph);
      }
      const txt = el("div");
      txt.appendChild(el("div", "item-name", item.name));
      txt.appendChild(el("div", "item-sku", item.sku));
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

      row.appendChild(el("div", "c-num", inr(item.price)));
      row.appendChild(el("div", "c-num", inr(item.mrp)));
      row.appendChild(el("div", "c-num", inr(saving)));
      row.appendChild(el("div", "c-num", inr(amount)));

      const del = el("button", "row-del");
      del.type = "button";
      del.innerHTML = '<i class="bi bi-trash"></i>';
      del.addEventListener("click", (e) => { e.stopPropagation(); removeItem(item.id); });
      row.appendChild(del);

      body.appendChild(row);
    });

    // Tax stays 0 until GST is built; the server will recalculate every total at checkout.
    const tax = 0;
    $("totItems").textContent = String(state.items.length);
    $("totSub").textContent = inr(subtotal);
    $("totTax").textContent = inr(tax);
    $("totGrand").textContent = inr(subtotal + tax);
  }

  // ---------- Actions ----------
  function focusSearch() { searchInput.focus(); searchInput.select(); }

  function focusQuantity() {
    if (!state.items.length) return;
    const id = state.selected || state.items[state.items.length - 1].id;
    state.selected = id;
    render();
  }

  function newSale() {
    if (state.items.length && !confirm("Start a new sale? The current bill will be cleared.")) return;
    clearCart();
    searchInput.value = "";
    $("searchNote").textContent = "";
    focusSearch();
  }

  function voidOrder() {
    if (!state.items.length) return;
    if (confirm("Void this order? All items will be removed.")) clearCart();
  }

  const actions = { search: focusSearch, quantity: focusQuantity, new: newSale, void: voidOrder };

  document.querySelectorAll("[data-action]").forEach((btn) => {
    btn.addEventListener("click", () => actions[btn.dataset.action]());
  });

  // Search: real lookup arrives in Phase 2.
  searchInput.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    if (!searchInput.value.trim()) return;
    $("searchNote").textContent = "Product search is not available yet. It starts working once Products are added.";
    searchInput.select();
  });

  // Keyboard shortcuts
  document.addEventListener("keydown", (e) => {
    const map = { F3: focusSearch, F4: focusQuantity, F8: newSale };
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

  window.POS = { addItem, clearCart };
  render();
  searchInput.focus();
})();