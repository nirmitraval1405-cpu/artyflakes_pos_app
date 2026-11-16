document.addEventListener("DOMContentLoaded", function () {
  const sidebar = document.getElementById("sidebar");
  const toggle = document.getElementById("sidebarToggle");
  const backdrop = document.getElementById("sidebarBackdrop");
  const fsBtn = document.getElementById("fullscreenBtn");

  const setSidebarOpen = (open) => {
    document.body.classList.toggle("sidebar-open", open);
    if (toggle) toggle.setAttribute("aria-expanded", String(open));
    if (open) {
      const firstLink = document.querySelector("#sidebar a:not(.disabled), #sidebar button:not(.disabled)");
      if (firstLink) firstLink.focus();
    } else if (toggle) {
      toggle.focus();
    }
  };

  if (toggle) toggle.addEventListener("click", () => setSidebarOpen(!document.body.classList.contains("sidebar-open")));
  if (backdrop) backdrop.addEventListener("click", () => setSidebarOpen(false));
  if (sidebar) {
    sidebar.addEventListener("click", (event) => {
      const link = event.target.closest("a[href]");
      if (
        link &&
        !link.classList.contains("disabled") &&
        window.matchMedia("(max-width: 991.98px)").matches &&
        document.body.classList.contains("sidebar-open")
      ) {
        setSidebarOpen(false);
      }
    });
  }
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && document.body.classList.contains("sidebar-open")) setSidebarOpen(false);
  });

  if (fsBtn) {
    fsBtn.addEventListener("click", () => {
      if (!document.fullscreenElement) document.documentElement.requestFullscreen();
      else document.exitFullscreen();
    });
  }

  const searchBox = document.getElementById("topbarSearch");
  const searchInput = document.getElementById("topbarFeatureSearch");
  const searchResults = document.getElementById("topbarSearchResults");
  if (sidebar && searchBox && searchInput && searchResults) {
    const seenLinks = new Set();
    const menuItems = Array.from(sidebar.querySelectorAll("a[href]")).map((link) => {
      const label = link.textContent.replace(/\s+/g, " ").trim();
      const group = link.closest(".collapse");
      const toggle = group && Array.from(sidebar.querySelectorAll(".nav-toggle"))
        .find((button) => button.dataset.bsTarget === "#" + group.id);
      const section = toggle ? toggle.textContent.replace(/\s+/g, " ").trim() : "";
      return {
        href: link.getAttribute("href"),
        label,
        section,
        searchText: (label + " " + section).toLocaleLowerCase(),
      };
    }).filter((item) => {
      if (!item.label || !item.href || seenLinks.has(item.href)) return false;
      seenLinks.add(item.href);
      return true;
    });

    function closeSearchResults() {
      searchResults.hidden = true;
      searchInput.setAttribute("aria-expanded", "false");
    }

    function renderSearchResults() {
      const query = searchInput.value.trim().toLocaleLowerCase();
      searchResults.replaceChildren();
      if (!query) {
        closeSearchResults();
        return [];
      }

      const matches = menuItems.filter((item) => item.searchText.includes(query));
      matches.slice(0, 10).forEach((item) => {
        const result = document.createElement("a");
        result.className = "topbar-search-result";
        result.href = item.href;
        result.setAttribute("role", "option");

        const label = document.createElement("span");
        label.className = "topbar-search-result-label";
        label.textContent = item.label;
        result.appendChild(label);

        if (item.section) {
          const section = document.createElement("small");
          section.className = "topbar-search-result-section";
          section.textContent = item.section;
          result.appendChild(section);
        }
        searchResults.appendChild(result);
      });

      if (!matches.length) {
        const empty = document.createElement("div");
        empty.className = "topbar-search-empty";
        empty.textContent = "No matching menu features.";
        searchResults.appendChild(empty);
      } else if (matches.length > 10) {
        const more = document.createElement("div");
        more.className = "topbar-search-empty";
        more.textContent = "Keep typing to narrow the results.";
        searchResults.appendChild(more);
      }

      searchResults.hidden = false;
      searchInput.setAttribute("aria-expanded", "true");
      return matches;
    }

    searchInput.addEventListener("input", renderSearchResults);
    searchInput.addEventListener("focus", renderSearchResults);
    searchInput.addEventListener("blur", () => setTimeout(() => {
      if (!searchBox.contains(document.activeElement)) closeSearchResults();
    }, 100));
    searchInput.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        closeSearchResults();
      } else if (event.key === "ArrowDown" && !searchResults.hidden) {
        event.preventDefault();
        const firstResult = searchResults.querySelector("a");
        if (firstResult) firstResult.focus();
      } else if (event.key === "Enter" && !searchResults.hidden) {
        const firstResult = searchResults.querySelector("a");
        if (firstResult) window.location.assign(firstResult.href);
      }
    });
    searchResults.addEventListener("keydown", (event) => {
      const results = Array.from(searchResults.querySelectorAll("a"));
      const currentIndex = results.indexOf(document.activeElement);
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        const direction = event.key === "ArrowDown" ? 1 : -1;
        const nextIndex = (currentIndex + direction + results.length) % results.length;
        results[nextIndex].focus();
      } else if (event.key === "Escape") {
        searchInput.focus();
        closeSearchResults();
      }
    });
    document.addEventListener("click", (event) => {
      if (!searchBox.contains(event.target)) closeSearchResults();
    });
  }
});