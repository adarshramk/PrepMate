/* PrepMate – main.js */
"use strict";

// ── Navbar hamburger toggle ───────────────────────────────────────────────
(function () {
  const toggle = document.getElementById("navToggle");
  const menu   = document.getElementById("navMenu");
  if (!toggle || !menu) return;

  toggle.addEventListener("click", function () {
    const isOpen = menu.classList.toggle("is-open");
    toggle.setAttribute("aria-expanded", isOpen ? "true" : "false");
  });

  // Close menu when a link is clicked (mobile UX)
  menu.querySelectorAll("a").forEach(function (link) {
    link.addEventListener("click", function () {
      menu.classList.remove("is-open");
      toggle.setAttribute("aria-expanded", "false");
    });
  });
})();

// ── Flash message dismiss ─────────────────────────────────────────────────
(function () {
  document.querySelectorAll(".flash-close").forEach(function (btn) {
    btn.addEventListener("click", function () {
      const flash = btn.closest(".flash");
      if (flash) {
        flash.style.transition = "opacity 0.3s ease";
        flash.style.opacity = "0";
        setTimeout(function () { flash.remove(); }, 310);
      }
    });
  });
})();

// ── Password visibility toggle ────────────────────────────────────────────
(function () {
  document.querySelectorAll(".toggle-password").forEach(function (btn) {
    btn.addEventListener("click", function () {
      const targetId = btn.getAttribute("data-target");
      const input    = document.getElementById(targetId);
      if (!input) return;

      const isPassword = input.type === "password";
      input.type = isPassword ? "text" : "password";
      btn.setAttribute("aria-label", isPassword ? "Hide password" : "Show password");
      btn.textContent = isPassword ? "🙈" : "👁";
    });
  });
})();

// ── Auto-dismiss flash messages after 5 s ────────────────────────────────
(function () {
  const flashes = document.querySelectorAll(".flash");
  flashes.forEach(function (flash) {
    setTimeout(function () {
      flash.style.transition = "opacity 0.5s ease";
      flash.style.opacity = "0";
      setTimeout(function () { flash.remove(); }, 510);
    }, 5000);
  });
})();
