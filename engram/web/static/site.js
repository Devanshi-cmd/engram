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
    var link = form.querySelector("input[name=url]");
    var zone = form.querySelector(".dropzone");
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
      if (link) link.disabled = false;
    }

    // A file or a link, never both: choosing one clears the other.
    if (link) {
      link.addEventListener("input", function () {
        var used = link.value.trim() !== "";
        zone.classList.toggle("is-off", used);
        if (used && input.value) {
          input.value = "";
          input.dispatchEvent(new Event("change"));
        }
      });
      input.addEventListener("change", function () {
        if (input.files && input.files[0]) {
          link.value = "";
          zone.classList.remove("is-off");
        }
      });
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
      var url = link ? link.value.trim() : "";
      if (!file && !url) return fail("Choose a lecture video, or paste a YouTube link.");
      error.hidden = true;
      if (!file) {
        button.disabled = true;
        button.textContent = "Checking the link…";
        link.disabled = true;
        var fields = new FormData(form);
        post("/links", {
          access_code: fields.get("access_code") || "", title: fields.get("title") || "", url: url,
        }).then(function (done) {
          window.location.assign(done.job);
        }).catch(function (e) { fail(e.message); });
        return;
      }
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

// "How it works?" walkthrough, as in DevSwarm. CSS draws and times every step; this only picks
// the step. Each dot's fill animation lasts as long as its step, and its end moves to the next.
(function () {
  document.documentElement.classList.add("js");
  var STATE = ["at", "locked", "check", "loop", "done"];

  function setup() {
    var root = document.querySelector(".hiw");
    if (!root) return;
    var stage = root.querySelector(".hiw-stage");
    var nodes = root.querySelectorAll(".hiw-node");
    var captions = root.querySelectorAll(".hiw-caption");
    var dots = root.querySelectorAll(".hiw-dot");
    var play = root.querySelector(".hiw-play");
    var current = 0;
    var paused = false;
    var hidden = true;

    function show(index) {
      current = index;
      var data = captions[index].dataset;
      STATE.forEach(function (name) {
        if (data[name]) stage.dataset[name] = data[name];
        else delete stage.dataset[name];
      });
      for (var i = 0; i < captions.length; i++) {
        captions[i].classList.toggle("is-current", i === index);
        dots[i].classList.toggle("is-current", i === index);
      }
      for (var j = 0; j < nodes.length; j++) {
        nodes[j].classList.toggle("is-active", nodes[j].dataset.node === data.at);
      }
    }

    function updatePaused() {
      root.classList.toggle("is-paused", paused || hidden);
      play.setAttribute("aria-pressed", String(paused));
      play.setAttribute("aria-label", paused ? "Play the walkthrough" : "Pause the walkthrough");
    }

    dots.forEach(function (dot, index) {
      dot.addEventListener("click", function () { show(index); });
    });
    play.addEventListener("click", function () {
      paused = !paused;
      updatePaused();
    });
    root.addEventListener("animationend", function (event) {
      if (event.animationName === "hiw-dot-fill") show((current + 1) % captions.length);
    });

    if ("IntersectionObserver" in window) {
      var reveal = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          // Anything already scrolled past counts as seen.
          if (!entry.isIntersecting && entry.boundingClientRect.top > 0) return;
          entry.target.classList.add("is-visible");
          reveal.unobserve(entry.target);
        });
      }, { threshold: 0.15 });
      root.querySelectorAll(".hiw-reveal").forEach(function (item) { reveal.observe(item); });
      // Only plays while on screen, so nobody scrolls past half the story.
      new IntersectionObserver(function (entries) {
        hidden = !entries[0].isIntersecting;
        updatePaused();
      }, { threshold: 0.35 }).observe(stage);
    } else {
      root.querySelectorAll(".hiw-reveal").forEach(function (item) { item.classList.add("is-visible"); });
      hidden = false;
    }
    updatePaused();
    show(0);
  }

  document.addEventListener("DOMContentLoaded", setup);
})();

// Home page: the hero card shows one sample course at a time and moves on every few seconds.
// It holds still while the pointer or keyboard focus is on it, and for reduced motion.
(function () {
  function setup() {
    var card = document.querySelector("[data-samples]");
    if (!card) return;
    var slides = card.querySelectorAll(".sample");
    var dots = card.querySelectorAll(".sample-dots button");
    if (slides.length < 2) return;
    var at = 0, timer = null, held = false;
    var still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    function show(i) {
      at = i;
      slides.forEach(function (slide, j) { slide.hidden = j !== i; });
      dots.forEach(function (dot, j) { dot.setAttribute("aria-current", String(j === i)); });
    }
    function schedule() {
      clearInterval(timer);
      if (still) return;
      timer = setInterval(function () {
        if (!held && !document.hidden) show((at + 1) % slides.length);
      }, 5000);
    }
    dots.forEach(function (dot, j) {
      dot.addEventListener("click", function () { show(j); schedule(); });
    });
    card.addEventListener("pointerenter", function () { held = true; });
    card.addEventListener("pointerleave", function () { held = false; });
    card.addEventListener("focusin", function () { held = true; });
    card.addEventListener("focusout", function () { held = false; });
    schedule();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", setup);
  else setup();
})();
