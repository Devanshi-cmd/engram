// The course page: one lecture player that every timestamp, frame, chapter and transcript
// word can seek, plus tabs, flashcards and the quiz. Also typesets maths (KaTeX) and draws
// diagrams (Mermaid) on the print page. Plain JavaScript, no build step.
(function () {
  "use strict";

  var LEAD = 2; // seconds of context before the moment a note cites

  function $(selector, root) { return (root || document).querySelector(selector); }
  function $$(selector, root) { return Array.prototype.slice.call((root || document).querySelectorAll(selector)); }
  function clock(t) {
    t = Math.floor(t);
    var h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
    var mm = (h && m < 10 ? "0" : "") + m, ss = (s < 10 ? "0" : "") + s;
    return h ? h + ":" + mm + ":" + ss : (m < 10 ? "0" : "") + m + ":" + ss;
  }
  function store(key, value) {
    try {
      if (value === undefined) return JSON.parse(localStorage.getItem(key) || "null");
      localStorage.setItem(key, JSON.stringify(value));
    } catch (error) { return null; }
    return null;
  }

  // ---- maths and diagrams ----------------------------------------------------------
  // The server marks maths as <span class="tex">; only those are typeset, so a stray
  // dollar sign in the text ("$1000s") is never mistaken for maths.
  function typeset(root) {
    if (!window.katex) return;
    $$(".tex", root).forEach(function (el) {
      if (el.dataset.done) return;
      el.dataset.done = "1";
      try {
        window.katex.render(el.textContent, el, { displayMode: el.classList.contains("tex-display"), throwOnError: false });
      } catch (error) { /* leave the LaTeX source showing */ }
    });
  }

  var mermaidReady = false;
  var diagramCount = 0;
  function drawDiagrams(root) {
    if (!window.mermaid) return;
    if (!mermaidReady) {
      var dark = document.documentElement.dataset.theme === "dark";
      window.mermaid.initialize({
        startOnLoad: false, securityLevel: "strict", theme: dark ? "dark" : "neutral",
        fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Inter, sans-serif",
      });
      mermaidReady = true;
    }
    $$(".diagram", root).forEach(function (figure) {
      var source = $(".mermaid-source", figure), view = $(".mermaid-view", figure);
      if (!source || !view || figure.dataset.drawn || figure.offsetParent === null) return;
      figure.dataset.drawn = "1";
      diagramCount += 1;
      window.mermaid.render("diagram-" + diagramCount, source.textContent).then(function (out) {
        view.innerHTML = out.svg; // Mermaid's strict mode sanitises its own output
        figure.classList.add("is-drawn");
      }).catch(function () {
        figure.classList.add("is-broken"); // the source stays visible instead
      });
    });
  }

  // ---- the lecture player ----------------------------------------------------------
  var player = $("#player");
  function seek(t, exact) {
    if (!player) return;
    player.currentTime = Math.max(0, t - (exact ? 0 : LEAD));
    var playing = player.play();
    if (playing && playing.catch) playing.catch(function () {});
    var box = player.getBoundingClientRect();
    if (box.bottom < 60 || box.top > window.innerHeight - 60) {
      player.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  }

  document.addEventListener("click", function (event) {
    var word = event.target.closest(".transcript [data-w]");
    if (word) { seek(transcript.starts[+word.dataset.w], true); return; }
    var target = event.target.closest("[data-t]");
    if (!target || !player) return;
    seek(parseFloat(target.dataset.t), target.classList.contains("chapter-link") || target.classList.contains("filmstrip-frame"));
  });

  // ---- live transcript -------------------------------------------------------------
  var transcript = { starts: [], spans: [], current: -1, scrolledAt: 0 };
  var box = $("#transcript");
  function buildTranscript(data) {
    var html = [];
    var index = 0;
    data.paragraphs.forEach(function (p) {
      var words = p.w.map(function (w) {
        transcript.starts.push(w[0]);
        var span = '<span data-w="' + index + '">' + escapeHtml(w[2]) + "</span>";
        index += 1;
        return span;
      });
      html.push('<p class="transcript-para' + (p.who ? " is-student" : "") + '"><button type="button" class="transcript-time num" data-t="' + p.t + '">' + clock(p.t) + "</button>" + (p.who ? '<span class="transcript-who">Student</span>' : "") + words.join(" ") + "</p>");
    });
    box.innerHTML = html.join("") || '<p class="muted">No speech was recognised in this lecture.</p>';
    transcript.spans = $$("[data-w]", box);
  }
  function escapeHtml(text) {
    return text.replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; });
  }
  function wordAt(t) {
    var s = transcript.starts, lo = 0, hi = s.length - 1, found = -1;
    while (lo <= hi) {
      var mid = (lo + hi) >> 1;
      if (s[mid] <= t) { found = mid; lo = mid + 1; } else { hi = mid - 1; }
    }
    return found;
  }
  if (box) {
    fetch(box.dataset.src).then(function (r) { return r.json(); }).then(buildTranscript).catch(function () {
      box.innerHTML = '<p class="muted">The transcript couldn\'t be loaded.</p>';
    });
    ["wheel", "touchmove", "keydown"].forEach(function (name) {
      box.addEventListener(name, function () { transcript.scrolledAt = Date.now(); }, { passive: true });
    });
  }

  // ---- follow the playhead: transcript word, chapter, filmstrip frame ----------------
  var chapters = $$(".chapter-link");
  var frames = $$(".filmstrip-frame");
  function follow() {
    if (!player) return;
    var t = player.currentTime;
    var i = wordAt(t);
    if (i !== transcript.current && transcript.spans.length) {
      if (transcript.current >= 0) transcript.spans[transcript.current].classList.remove("is-current");
      transcript.current = i;
      if (i >= 0) {
        var span = transcript.spans[i];
        span.classList.add("is-current");
        if (Date.now() - transcript.scrolledAt > 4000) {
          var top = span.offsetTop - box.offsetTop - box.clientHeight / 3;
          box.scrollTo({ top: top, behavior: "smooth" });
        }
      }
    }
    chapters.forEach(function (link) {
      var on = t >= parseFloat(link.dataset.t) && t < parseFloat(link.dataset.end);
      link.classList.toggle("is-current", on);
      if (on) link.setAttribute("aria-current", "true"); else link.removeAttribute("aria-current");
    });
    var shown = null;
    frames.forEach(function (f) { if (parseFloat(f.dataset.t) <= t + 0.5) shown = f; });
    frames.forEach(function (f) { f.classList.toggle("is-current", f === shown); });
    if (shown && !shown.dataset.seen) {
      frames.forEach(function (f) { delete f.dataset.seen; });
      shown.dataset.seen = "1";
      var strip = shown.parentElement;
      strip.scrollTo({ left: shown.offsetLeft - strip.clientWidth / 2 + shown.clientWidth / 2, behavior: "smooth" });
    }
  }
  if (player) {
    player.addEventListener("timeupdate", follow);
    player.addEventListener("seeked", follow);
    var tick = function () { if (!player.paused) { follow(); requestAnimationFrame(tick); } };
    player.addEventListener("play", function () { requestAnimationFrame(tick); });
  }

  // ---- tabs ------------------------------------------------------------------------
  var tabs = $$('[role="tab"]');
  function showTab(tab, focus) {
    tabs.forEach(function (other) {
      var on = other === tab;
      other.setAttribute("aria-selected", String(on));
      other.tabIndex = on ? 0 : -1;
      document.getElementById(other.getAttribute("aria-controls")).hidden = !on;
    });
    if (focus) tab.focus();
    var panel = document.getElementById(tab.getAttribute("aria-controls"));
    drawDiagrams(panel);
    if (history.replaceState) history.replaceState(null, "", "#" + panel.id);
  }
  tabs.forEach(function (tab, i) {
    tab.addEventListener("click", function () { showTab(tab, false); });
    tab.addEventListener("keydown", function (event) {
      var step = { ArrowRight: 1, ArrowLeft: -1 }[event.key];
      if (step) { event.preventDefault(); showTab(tabs[(i + step + tabs.length) % tabs.length], true); }
    });
  });
  var wanted = tabs.filter(function (t) { return "#" + t.getAttribute("aria-controls") === location.hash; })[0];
  if (wanted) showTab(wanted, false);
  // A chapter link from the sidebar always lands in the notes.
  chapters.forEach(function (link) {
    link.addEventListener("click", function () { if (tabs[0]) showTab(tabs[0], false); });
  });

  // ---- flashcards ------------------------------------------------------------------
  var deck = $(".deck");
  if (deck) {
    var course = $(".course").dataset.course;
    var key = "engram:" + course + ":known";
    var known = store(key) || {};
    var all = $$(".flashcard", deck);
    var queue = [], flipped = false;
    var flip = $(".deck-flip", deck), again = $(".deck-again", deck), know = $(".deck-know", deck);
    var progress = $(".deck-progress", deck), filter = $(".deck-filter", deck), done = $(".deck-done", deck);
    deck.classList.add("is-live");
    $(".deck-controls", deck).hidden = false;

    var start = function () {
      var chapter = filter.value;
      queue = all.filter(function (card) {
        return (chapter === "all" || card.dataset.chapter === chapter) && !known[card.dataset.id];
      });
      show();
    };
    var show = function () {
      flipped = false;
      all.forEach(function (card) { card.classList.remove("is-shown", "is-flipped"); });
      var chapter = filter.value;
      var total = all.filter(function (c) { return chapter === "all" || c.dataset.chapter === chapter; }).length;
      done.hidden = queue.length > 0;
      flip.hidden = queue.length === 0;
      again.hidden = know.hidden = true;
      progress.textContent = (total - queue.length) + " of " + total + " known";
      if (!queue.length) return;
      queue[0].classList.add("is-shown");
      typeset(queue[0]);
    };
    var turn = function () {
      if (!queue.length || flipped) return;
      flipped = true;
      queue[0].classList.add("is-flipped");
      flip.hidden = true;
      again.hidden = know.hidden = false;
      know.focus();
    };
    var answer = function (gotIt) {
      if (!queue.length || !flipped) return;
      var card = queue.shift();
      if (gotIt) { known[card.dataset.id] = 1; store(key, known); } else { queue.push(card); }
      show();
      flip.focus();
    };
    flip.addEventListener("click", turn);
    again.addEventListener("click", function () { answer(false); });
    know.addEventListener("click", function () { answer(true); });
    filter.addEventListener("change", start);
    $(".deck-restart", deck).addEventListener("click", function () {
      all.forEach(function (card) {
        if (filter.value === "all" || card.dataset.chapter === filter.value) delete known[card.dataset.id];
      });
      store(key, known);
      start();
    });
    document.addEventListener("keydown", function (event) {
      if (deck.closest("[hidden]") || /INPUT|SELECT|TEXTAREA/.test(event.target.tagName)) return;
      if (event.key === " " && !flipped && queue.length) { event.preventDefault(); turn(); }
      else if ((event.key === "1" || event.key === "ArrowLeft") && flipped) answer(false);
      else if ((event.key === "2" || event.key === "ArrowRight") && flipped) answer(true);
    });
    start();
  }

  // ---- quiz ------------------------------------------------------------------------
  var quiz = $(".quiz");
  if (quiz) {
    var questions = $$(".question", quiz);
    var score = $(".quiz-score", quiz);
    var answered = 0, right = 0;
    quiz.classList.add("is-live");
    questions.forEach(function (q) {
      var result = $(".question-result", q);
      $$(".option", q).forEach(function (option) {
        option.addEventListener("click", function () {
          if (q.dataset.done) return;
          q.dataset.done = "1";
          var correct = +option.dataset.index === +q.dataset.answer;
          answered += 1;
          if (correct) right += 1;
          $$(".option", q).forEach(function (o) {
            o.disabled = true;
            if (+o.dataset.index === +q.dataset.answer) o.classList.add("is-right");
          });
          if (!correct) option.classList.add("is-wrong");
          q.classList.add(correct ? "was-right" : "was-wrong");
          $(".question-verdict", q).textContent = correct ? "Right." : "Not quite. The answer is " + "ABCD"[+q.dataset.answer] + ".";
          result.open = true;
          score.textContent = right + " of " + answered + " right" + (answered < questions.length ? " · " + (questions.length - answered) + " to go" : ". Done.");
        });
      });
    });
  }

  // ---- start -----------------------------------------------------------------------
  function ready() {
    typeset(document.body);
    drawDiagrams(document);
  }
  if (document.readyState === "complete") ready(); else window.addEventListener("load", ready);
})();
