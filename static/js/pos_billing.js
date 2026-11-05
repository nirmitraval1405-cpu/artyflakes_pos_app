(function () {
  "use strict";

  const state = {
    items: [],
    selected: null,
    customerName: "",
    customerPhone: "",
    submitting: false,
  };
  const $ = (id) => document.getElementById(id);
  const body = $("cartBody");
  const empty = $("cartEmpty");
  const searchInput = $("posSearch");
  const results = $("productResults");
  const csrfToken = document.querySelector("[name=csrfmiddlewaretoken]").value;
  const paymentDialog = $("paymentDialog");
  const receiptDialog = $("receiptDialog");
  let searchTimer;
  let searchSequence = 0;
  let searchStartedAt = 0;
  let latestResults = [];

  const money = (paise) =>
    "₹ " + (paise / 100).toLocaleString("en-IN", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
  const toPaise = (value) => Math.round(Number(value || 0) * 100);
  const fromPaise = (value) => (value / 100).toFixed(2);
  const roundMoney = (value) => Math.round(value);

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function cartTotals() {
    return state.items.reduce((totals, item) => {
      const lineSubtotal = roundMoney(item.price * item.qty);
      const lineDiscount = roundMoney(item.discount * item.qty);
      const lineTax = roundMoney((lineSubtotal - lineDiscount) * item.gstRate / 100);
      totals.subtotal += lineSubtotal;
      totals.discount += lineDiscount;
      totals.tax += lineTax;
      totals.quantity += item.qty;
      return totals;
    }, { subtotal: 0, discount: 0, tax: 0, quantity: 0 });
  }

  function itemFromProduct(product) {
    return {
      id: product.id,
      name: product.name,
      sku: product.sku || "",
      barcode: product.barcode || "",
      price: toPaise(product.price),
      mrp: toPaise(product.mrp || product.price),
      gstRate: Number(product.gst_rate || 0),
      stock: Number(product.stock || 0),
      trackStock: Boolean(product.track_stock),
      qty: 1,
      discount: 0,
    };
  }

  function addItem(product) {
    const item = state.items.find((entry) => entry.id === product.id);
    const currentQuantity = item ? item.qty : 0;
    const stock = Number(product.stock || (item && item.stock) || 0);
    const trackStock = product.track_stock === undefined
      ? Boolean(item && item.trackStock)
      : Boolean(product.track_stock);
    if (trackStock && currentQuantity + 1 > stock) {
      $("searchNote").textContent = `Insufficient stock. Available: ${stock}.`;
      return;
    }

    if (item) {
      item.qty += 1;
    } else {
      state.items.push(itemFromProduct(product));
    }
    state.selected = product.id;
    $("searchNote").textContent = "";
    render();
  }

  function setQuantity(id, quantity) {
    const item = state.items.find((entry) => entry.id === id);
    if (!item) return;
    if (!Number.isFinite(quantity) || quantity <= 0 || !Number.isInteger(quantity * 1000)) {
      window.alert("Enter a quantity greater than zero with up to 3 decimal places.");
      return;
    }
    if (item.trackStock && quantity > item.stock) {
      window.alert(`Insufficient stock. Available: ${item.stock}.`);
      return;
    }
    item.qty = quantity;
    render();
  }

  function changeQty(id, delta) {
    const item = state.items.find((entry) => entry.id === id);
    if (item) setQuantity(id, item.qty + delta);
  }

  function removeItem(id) {
    state.items = state.items.filter((item) => item.id !== id);
    if (state.selected === id) state.selected = state.items.length ? state.items[0].id : null;
    render();
  }

  function clearCart(resetCustomer) {
    state.items = [];
    state.selected = null;
    if (resetCustomer) {
      state.customerName = "";
      state.customerPhone = "";
      $("customerDisplay").textContent = "Walk-in";
    }
    render();
  }

  function render() {
    body.querySelectorAll(".cart-row").forEach((row) => row.remove());
    empty.style.display = state.items.length ? "none" : "flex";

    state.items.forEach((item) => {
      const lineAmount = roundMoney((item.price - item.discount) * item.qty);
      const row = element("div", "cart-row" + (item.id === state.selected ? " selected" : ""));
      row.addEventListener("click", () => {
        state.selected = item.id;
        render();
      });

      const info = element("div", "item-info");
      info.appendChild(element("div", "item-img", "▧"));
      const text = element("div");
      text.appendChild(element("div", "item-name", item.name));
      text.appendChild(element("div", "item-sku", item.sku));
      info.appendChild(text);
      row.appendChild(info);

      const quantity = element("div");
      const box = element("div", "qty-box");
      const minus = element("button", "", "−");
      const plus = element("button", "", "+");
      minus.type = plus.type = "button";
      minus.setAttribute("aria-label", `Decrease ${item.name}`);
      plus.setAttribute("aria-label", `Increase ${item.name}`);
      minus.addEventListener("click", (event) => {
        event.stopPropagation();
        changeQty(item.id, -1);
      });
      plus.addEventListener("click", (event) => {
        event.stopPropagation();
        addItem(item);
      });
      box.append(minus, element("span", "", String(item.qty)), plus);
      quantity.appendChild(box);
      row.appendChild(quantity);

      row.appendChild(element("div", "c-num", money(item.price)));
      row.appendChild(element("div", "c-num", money(item.mrp)));
      row.appendChild(element("div", "c-num", money(item.discount)));
      row.appendChild(element("div", "c-num", money(lineAmount)));

      const remove = element("button", "row-del", "×");
      remove.type = "button";
      remove.setAttribute("aria-label", `Remove ${item.name}`);
      remove.addEventListener("click", (event) => {
        event.stopPropagation();
        removeItem(item.id);
      });
      row.appendChild(remove);
      body.appendChild(row);
    });

    const totals = cartTotals();
    $("totItems").textContent = totals.quantity.toLocaleString("en-IN", { maximumFractionDigits: 3 });
    $("totSub").textContent = money(totals.subtotal);
    $("totDiscount").textContent = money(totals.discount);
    $("totTax").textContent = money(totals.tax);
    $("totGrand").textContent = money(totals.subtotal - totals.discount + totals.tax);
  }

  async function api(url, options) {
    const response = await fetch(url, {
      credentials: "same-origin",
      ...options,
      headers: {
        "X-CSRFToken": csrfToken,
        ...(options && options.headers),
      },
    });
    let data;
    try {
      data = await response.json();
    } catch (error) {
      throw new Error("The server returned an unexpected response. Please refresh and try again.");
    }
    if (!response.ok) throw new Error(data.error || "The request could not be completed.");
    return data;
  }

  async function searchProducts(query) {
    const sequence = ++searchSequence;
    try {
      const data = await api(`/api/products/search/?q=${encodeURIComponent(query)}`, { method: "GET" });
      if (sequence !== searchSequence) return [];
      latestResults = data.results;
      showResults(data.results);
      return data.results;
    } catch (error) {
      if (sequence === searchSequence) {
        latestResults = [];
        showResults([]);
        $("searchNote").textContent = error.message;
      }
      return [];
    }
  }

  function showResults(products) {
    results.replaceChildren();
    if (!searchInput.value.trim()) {
      results.hidden = true;
      return;
    }
    if (!products.length) {
      results.appendChild(element("div", "product-search-empty", "No matching products."));
      results.hidden = false;
      return;
    }
    products.forEach((product) => {
      const button = element("button", "product-result");
      button.type = "button";
      button.setAttribute("role", "option");
      const name = element("span");
      name.appendChild(element("strong", "", product.name));
      name.appendChild(element("small", "", product.sku || product.barcode || "No code"));
      const stockText = product.track_stock ? `Stock: ${product.stock}` : "Not tracked";
      const stock = element("span", "product-result-stock");
      stock.appendChild(element("strong", "", money(toPaise(product.price))));
      stock.appendChild(element("small", "", stockText));
      button.append(name, stock);
      button.addEventListener("click", () => {
        addItem(product);
        searchInput.value = "";
        searchStartedAt = 0;
        latestResults = [];
        showResults([]);
        focusSearch();
      });
      results.appendChild(button);
    });
    results.hidden = false;
  }

  async function submitSearch(rapidScan) {
    const query = searchInput.value.trim();
    if (!query) return;
    let products = latestResults;
    if (!products.length) products = await searchProducts(query);
    const exact = products.find((product) =>
      product.barcode.toLowerCase() === query.toLowerCase()
      || product.sku.toLowerCase() === query.toLowerCase()
    );
    if (exact || (rapidScan && products.length === 1)) {
      addItem(exact || products[0]);
      searchInput.value = "";
      searchStartedAt = 0;
      results.hidden = true;
      latestResults = [];
    } else if (products.length > 1) {
      $("searchNote").textContent = "Select a product from the matching results.";
    }
  }

  function focusSearch() {
    searchInput.focus();
    searchInput.select();
  }

  function focusQuantity() {
    if (!state.items.length) return;
    const item = state.items.find((entry) => entry.id === state.selected) || state.items[state.items.length - 1];
    const input = window.prompt(`Quantity for ${item.name}:`, String(item.qty));
    if (input === null) return;
    setQuantity(item.id, Number(input));
  }

  function newSale() {
    if (state.items.length && !window.confirm("Start a new sale? The current bill will be cleared.")) return;
    clearCart(true);
    searchInput.value = "";
    $("searchNote").textContent = "";
    focusSearch();
  }

  function voidOrder() {
    if (!state.items.length) return;
    if (window.confirm("Void this order? All items will be removed.")) clearCart(true);
  }

  function applyDiscount() {
    if (!state.items.length) {
      window.alert("Add an item before applying a discount.");
      return;
    }
    const scope = state.items.length > 1
      ? window.prompt("Apply percentage discount to which items? Enter 'selected' or 'all'.", "selected")
      : "selected";
    if (scope === null) return;
    if (scope !== "selected" && scope !== "all") {
      window.alert("Enter 'selected' or 'all'.");
      return;
    }
    const enteredPercentage = window.prompt("Discount percentage (0–100):", "0");
    if (enteredPercentage === null) return;
    const percentage = Number(enteredPercentage);
    if (!Number.isFinite(percentage) || percentage < 0 || percentage > 100) {
      window.alert("Enter a discount percentage from 0 to 100.");
      return;
    }
    const targets = scope === "all"
      ? state.items
      : [state.items.find((item) => item.id === state.selected) || state.items[state.items.length - 1]];
    targets.forEach((item) => {
      item.discount = Math.round(item.price * percentage / 100);
    });
    render();
  }

  function attachCustomer() {
    const name = window.prompt("Customer name (leave blank for a walk-in sale):", state.customerName);
    if (name === null) return;
    const phone = window.prompt("Customer phone number (optional):", state.customerPhone);
    if (phone === null) return;
    state.customerName = name.trim();
    state.customerPhone = phone.trim();
    $("customerDisplay").textContent = state.customerName || state.customerPhone || "Walk-in";
  }

  function formatReceipt(sale) {
    const lines = [
      "ARTYFLAKES POS",
      `Receipt: ${sale.order_number}`,
      `Date: ${new Date(sale.created_at).toLocaleString("en-IN")}`,
      sale.customer ? `Customer: ${sale.customer}` : "Customer: Walk-in",
      "",
      ...sale.items.map((item) =>
        `${item.name}  x${item.quantity}  ${money(toPaise(item.total))}`
      ),
      "",
      `Subtotal: ${money(toPaise(sale.subtotal))}`,
      `Discount: ${money(toPaise(sale.discount))}`,
      `Tax: ${money(toPaise(sale.tax))}`,
      `TOTAL: ${money(toPaise(sale.total))}`,
      `Paid by: ${sale.payment_method.toUpperCase()}`,
      "",
      "Thank you for shopping with us!",
    ];
    return lines.join("\n");
  }

  async function checkout(method, paymentDetails) {
    if (state.submitting) return;
    if (!state.items.length) {
      window.alert("Add at least one product before checkout.");
      return;
    }
    state.submitting = true;
    try {
      const data = await api("/api/pos/checkout/", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          items: state.items.map((item) => ({
            id: item.id,
            qty: item.qty,
            discount: fromPaise(item.discount),
          })),
          payment_method: method,
          payment_details: paymentDetails || {},
          customer_name: state.customerName,
          customer_phone: state.customerPhone,
        }),
      });
      $("receiptContent").textContent = formatReceipt(data.sale);
      clearCart(true);
      searchInput.value = "";
      results.hidden = true;
      focusSearch();
      receiptDialog.showModal();
    } catch (error) {
      window.alert(error.message);
    } finally {
      state.submitting = false;
    }
  }

  function openPaymentDialog() {
    if (!state.items.length) {
      window.alert("Add at least one product before checkout.");
      return;
    }
    const totals = cartTotals();
    $("paymentDue").textContent = money(totals.subtotal - totals.discount + totals.tax);
    $("paymentMethod").value = "cash";
    $("splitFields").hidden = true;
    if (!paymentDialog.open) paymentDialog.showModal();
  }

  async function holdCart() {
    if (!state.items.length) {
      window.alert("Add at least one product before saving a sale.");
      return;
    }
    try {
      await api("/api/pos/hold/", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          items: state.items,
          customer_name: state.customerName,
          customer_phone: state.customerPhone,
        }),
      });
      clearCart(true);
      searchInput.value = "";
      focusSearch();
      window.alert("Sale saved. Restore it from More → View held sales.");
    } catch (error) {
      window.alert(error.message);
    }
  }

  async function restoreHeld() {
    try {
      const data = await api("/api/pos/held/", { method: "GET" });
      if (!data.results.length) {
        window.alert("There are no held sales.");
        return;
      }
      const choices = data.results.map((sale, index) =>
        `${index + 1}. ${sale.customer_name || "Walk-in"} — ${sale.items.length} item(s)`
      ).join("\n");
      const choice = Number(window.prompt(`Choose a sale to restore:\n${choices}`, "1"));
      if (!Number.isInteger(choice) || choice < 1 || choice > data.results.length) return;
      const held = data.results[choice - 1];
      if (state.items.length && !window.confirm("Replace the current cart with this held sale?")) return;
      const restored = await api(`/api/pos/held/${held.id}/restore/`, { method: "POST" });
      state.items = restored.items.map((item) => ({
        ...item,
        id: Number(item.id),
        qty: Number(item.qty),
        price: Number(item.price),
        mrp: Number(item.mrp),
        discount: Number(item.discount || 0),
        gstRate: Number(item.gstRate || 0),
        stock: Number(item.stock || 0),
        trackStock: Boolean(item.trackStock),
      }));
      state.customerName = restored.customer_name || "";
      state.customerPhone = restored.customer_phone || "";
      state.selected = state.items.length ? state.items[0].id : null;
      $("customerDisplay").textContent = state.customerName || state.customerPhone || "Walk-in";
      render();
      focusSearch();
    } catch (error) {
      window.alert(error.message);
    }
  }

  function showUnavailable(feature) {
    window.alert(`${feature} is not implemented yet.`);
  }

  function openMenu() {
    $("sidePanel").classList.add("open");
    $("sideBackdrop").classList.add("open");
    $("sidePanel").setAttribute("aria-hidden", "false");
  }

  function closeMenu() {
    $("sidePanel").classList.remove("open");
    $("sideBackdrop").classList.remove("open");
    $("sidePanel").setAttribute("aria-hidden", "true");
  }

  $("sideClose").addEventListener("click", closeMenu);
  $("sideBackdrop").addEventListener("click", closeMenu);
  $("userInfoBtn").addEventListener("click", () => {
    const box = $("userInfoBox");
    box.hidden = !box.hidden;
  });
  $("fsBtn").addEventListener("click", () => {
    if (!document.fullscreenElement) document.documentElement.requestFullscreen();
    else document.exitFullscreen();
  });

  const actions = {
    search: focusSearch,
    quantity: focusQuantity,
    new: newSale,
    void: voidOrder,
    menu: openMenu,
    cash: () => checkout("cash"),
    card: () => checkout("card"),
    check: () => checkout("check"),
    payment: openPaymentDialog,
    discount: applyDiscount,
    customer: attachCustomer,
    hold: holdCart,
    held: restoreHeld,
    refund: () => showUnavailable("Refunds"),
    drawer: () => showUnavailable("Cash drawer control"),
    transfer: () => showUnavailable("Sale transfer"),
    lock: () => showUnavailable("Register lock"),
    price: () => showUnavailable("Price overrides"),
  };

  document.querySelectorAll("[data-action]").forEach((button) => {
    button.addEventListener("click", () => {
      const action = actions[button.dataset.action];
      if (action) action();
    });
  });

  searchInput.addEventListener("input", () => {
    window.clearTimeout(searchTimer);
    searchSequence += 1;
    latestResults = [];
    $("searchNote").textContent = "";
    const query = searchInput.value.trim();
    if (!query) {
      searchStartedAt = 0;
      results.hidden = true;
      return;
    }
    if (!searchStartedAt) searchStartedAt = performance.now();
    searchTimer = window.setTimeout(() => searchProducts(query), 220);
  });

  searchInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      window.clearTimeout(searchTimer);
      const rapidScan = searchStartedAt > 0
        && performance.now() - searchStartedAt <= 600
        && searchInput.value.trim().length >= 4;
      submitSearch(rapidScan);
    } else if (event.key === "ArrowDown" && !results.hidden) {
      event.preventDefault();
      const first = results.querySelector(".product-result");
      if (first) first.focus();
    } else if (event.key === "Escape") {
      results.hidden = true;
    }
  });

  $("scanBtn").addEventListener("click", focusSearch);
  document.addEventListener("click", (event) => {
    if (!event.target.closest(".pos-search-wrap")) results.hidden = true;
  });

  $("paymentMethod").addEventListener("change", (event) => {
    $("splitFields").hidden = event.target.value !== "split";
  });
  $("paymentCancel").addEventListener("click", () => paymentDialog.close());
  $("paymentForm").addEventListener("submit", (event) => {
    event.preventDefault();
    const method = $("paymentMethod").value;
    const details = method === "split" ? {
      cash: Number($("payCash").value || 0).toFixed(2),
      card: Number($("payCard").value || 0).toFixed(2),
      upi: Number($("payUpi").value || 0).toFixed(2),
    } : {};
    paymentDialog.close();
    checkout(method, details);
  });
  $("receiptClose").addEventListener("click", () => {
    receiptDialog.close();
    focusSearch();
  });
  $("receiptPrint").addEventListener("click", () => window.print());
  paymentDialog.addEventListener("click", (event) => {
    if (event.target === paymentDialog) paymentDialog.close();
  });
  receiptDialog.addEventListener("click", (event) => {
    if (event.target === receiptDialog) receiptDialog.close();
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeMenu();
    if (paymentDialog.open || receiptDialog.open) return;
    const shortcuts = {
      F2: applyDiscount,
      F3: focusSearch,
      F4: focusQuantity,
      F7: () => showUnavailable("Sale transfer"),
      F8: newSale,
      F9: holdCart,
      F10: openPaymentDialog,
      F12: () => checkout("cash"),
    };
    if (shortcuts[event.key]) {
      event.preventDefault();
      shortcuts[event.key]();
      return;
    }
    if ((event.key === "+" || event.key === "-")
        && document.activeElement !== searchInput && state.selected) {
      event.preventDefault();
      changeQty(state.selected, event.key === "+" ? 1 : -1);
    }
  });

  function tick() {
    const now = new Date();
    $("posDate").textContent = now.toLocaleDateString("en-GB", {
      day: "2-digit", month: "short", year: "numeric",
    });
    $("posTime").textContent = now.toLocaleTimeString("en-IN", {
      hour: "2-digit", minute: "2-digit", hour12: true,
    });
  }

  tick();
  window.setInterval(tick, 30000);
  window.POS = { addItem, clearCart: () => clearCart(false) };
  render();
  focusSearch();
})();
