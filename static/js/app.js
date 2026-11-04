document.addEventListener("DOMContentLoaded", function () {
  const toggle = document.getElementById("sidebarToggle");
  const backdrop = document.getElementById("sidebarBackdrop");
  const fsBtn = document.getElementById("fullscreenBtn");

  if (toggle) toggle.addEventListener("click", () => document.body.classList.toggle("sidebar-open"));
  if (backdrop) backdrop.addEventListener("click", () => document.body.classList.remove("sidebar-open"));

  if (fsBtn) {
    fsBtn.addEventListener("click", () => {
      if (!document.fullscreenElement) document.documentElement.requestFullscreen();
      else document.exitFullscreen();
    });
  }
});