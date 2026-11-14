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

// Upload: the access code is checked first, and only then is the file sent, straight to
// storage through a one-time link. A wrong code never uploads a byte.
(function () {
  function setup() {
    var form = document.querySelector("form[data-upload]");
    if (!form) return;
    var input = form.querySelector("input[type=file]");
    var hint = form.querySelector(".dropzone-hint");
    var error = form.querySelector("[data-upload-error]");
    var button = form.querySelector("button[type=submit]");
    var label = button.innerHTML;

    function fail(message) {
      error.textContent = message;
      error.hidden = false;
      button.disabled = false;
      button.innerHTML = label;
      input.disabled = false;
    }
    function post(url, body) {
      return fetch(url, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body || {}), credentials: "same-origin",
      }).then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (data) {
          if (!r.ok) throw new Error(data.error || "Something went wrong. Try again.");
          return data;
        });
      });
    }
    function send(slot, file) {
      return new Promise(function (resolve, reject) {
        var xhr = new XMLHttpRequest();
        xhr.open("PUT", slot.put_url);
        xhr.setRequestHeader("Content-Type", slot.content_type);
        xhr.upload.addEventListener("progress", function (e) {
          if (e.lengthComputable) hint.textContent = "Uploading… " + Math.floor(e.loaded / e.total * 100) + "%";
        });
        xhr.addEventListener("load", function () {
          if (xhr.status >= 200 && xhr.status < 300) resolve();
          else reject(new Error("The upload was refused (" + xhr.status + "). Try again."));
        });
        xhr.addEventListener("error", function () { reject(new Error("The upload was interrupted. Check your connection and try again.")); });
        xhr.send(file);
      });
    }

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var file = input.files && input.files[0];
      if (!file) return fail("Choose a lecture video first.");
      error.hidden = true;
      button.disabled = true;
      button.textContent = "Checking the code…";
      var data = new FormData(form);
      post("/uploads", {
        access_code: data.get("access_code") || "", title: data.get("title") || "",
        filename: file.name, size: file.size,
      }).then(function (slot) {
        button.textContent = "Uploading…";
        input.disabled = true;
        return send(slot, file).then(function () {
          button.textContent = "Starting…";
          return post("/uploads/" + slot.id + "/done");
        });
      }).then(function (done) {
        window.location.assign(done.job);
      }).catch(function (e) { fail(e.message); });
    });
  }

  document.addEventListener("DOMContentLoaded", setup);
})();
