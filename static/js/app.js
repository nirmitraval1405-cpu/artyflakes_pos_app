document.addEventListener("DOMContentLoaded", function () {
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
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && document.body.classList.contains("sidebar-open")) setSidebarOpen(false);
  });

  if (fsBtn) {
    fsBtn.addEventListener("click", () => {
      if (!document.fullscreenElement) document.documentElement.requestFullscreen();
      else document.exitFullscreen();
    });
  }
});