// Light by default. The header's switch slides to night mode and remembers the choice.
// Same behaviour as DevSwarm's dashboard, so the two products feel like one family.
(function () {
  var root = document.documentElement;
  try {
    if (localStorage.getItem("theme") === "dark") root.dataset.theme = "dark";
  } catch (error) {}

  function showTheme() {
    var toggle = document.querySelector(".theme-switch");
    if (toggle) toggle.setAttribute("aria-checked", String(root.dataset.theme === "dark"));
  }

  document.addEventListener("click", function (event) {
    var faq = document.querySelector(".faq[open]");
    if (faq && !faq.contains(event.target)) faq.open = false;

    if (!event.target.closest(".theme-switch")) return;
    var next = root.dataset.theme === "dark" ? "light" : "dark";
    if (next === "dark") root.dataset.theme = "dark";
    else delete root.dataset.theme;
    showTheme();
    try { localStorage.setItem("theme", next); } catch (error) {}
  });

  document.addEventListener("keydown", function (event) {
    var faq = document.querySelector(".faq[open]");
    if (event.key === "Escape" && faq) {
      faq.open = false;
      faq.querySelector("summary").focus();
    }
  });

  document.addEventListener("DOMContentLoaded", showTheme);
})();

// Upload drop zone: shows the chosen file's name and highlights while a file is dragged over.
(function () {
  function setup() {
    var zone = document.querySelector(".dropzone");
    if (!zone) return;
    var input = zone.querySelector("input[type=file]");
    var title = zone.querySelector(".dropzone-title");
    var hint = zone.querySelector(".dropzone-hint");
    var defaultTitle = title.textContent;
    var defaultHint = hint.textContent;

    function show() {
      var file = input.files && input.files[0];
      zone.classList.toggle("has-file", Boolean(file));
      title.textContent = file ? file.name : defaultTitle;
      hint.textContent = file ? (file.size / 1048576).toFixed(0) + " MB · choose another" : defaultHint;
    }

    ["dragenter", "dragover"].forEach(function (name) {
      zone.addEventListener(name, function () { zone.classList.add("is-dragging"); });
    });
    ["dragleave", "drop"].forEach(function (name) {
      zone.addEventListener(name, function () { zone.classList.remove("is-dragging"); });
    });
    input.addEventListener("change", show);
    show();
  }

  document.addEventListener("DOMContentLoaded", setup);
})();
