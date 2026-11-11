/* Document review component for the contract MVP.
 *
 * Streamlit custom component (no build step): the iframe receives a JSON payload
 * with the document, inline annotations and tracked changes, and posts user
 * intents back through the component value.
 */
(function () {
  "use strict";

  var app = document.getElementById("app");
  var state = {
    data: null,
    activeColor: null,
    selection: null,
    selMode: "comment",
    openThreadId: null,
    showResolved: true,
    scrollY: 0
  };

  /* ---------------- Streamlit bridge ---------------- */
  function send(type, extra) {
    window.parent.postMessage(Object.assign({ isStreamlitMessage: true, type: type }, extra || {}), "*");
  }

  var lastHeight = 0;
  function resize() {
    // Measure the content wrapper, never body: body grows with the iframe itself,
    // which would feed the height back into Streamlit and make the frame run away.
    var content = document.getElementById("app");
    if (!content) return;
    var height = Math.ceil(content.getBoundingClientRect().height) + 10;
    if (height < 60) return;
    if (Math.abs(height - lastHeight) < 2) return;
    lastHeight = height;
    send("streamlit:setFrameHeight", { height: height });
  }

  function emit(action) {
    action.nonce = Date.now() + "-" + Math.random().toString(36).slice(2);
    send("streamlit:setComponentValue", { value: action, dataType: "json" });
  }

  window.addEventListener("message", function (event) {
    var payload = event.data;
    if (!payload || payload.type !== "streamlit:render") return;
    var args = payload.args || {};
    if (!args.payload) return;
    var first = state.data === null;
    state.scrollY = first ? 0 : window.scrollY;
    state.data = args.payload;
    render();
    requestAnimationFrame(function () {
      resize();
      if (!first) window.scrollTo(0, state.scrollY);
    });
  });

  /* ---------------- helpers ---------------- */
  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function initials(name) {
    var parts = String(name || "?").trim().split(/\s+/);
    var value = parts.length > 1 ? parts[0][0] + parts[parts.length - 1][0] : parts[0].slice(0, 2);
    return value.toUpperCase();
  }

  function sectionById(id) {
    return (state.data.sections || []).filter(function (s) { return s.id === id; })[0] || null;
  }

  function numberFor(id) {
    var list = state.data.annotations || [];
    for (var i = 0; i < list.length; i++) if (list[i].id === id) return i + 1;
    return 0;
  }

  function decorationsFor(sectionId) {
    var comments = (state.data.annotations || [])
      .filter(function (a) { return a.section_id === sectionId && a.start !== null && a.end !== null; })
      .filter(function (a) { return state.showResolved || !a.resolved; })
      .map(function (a) {
        return { kind: "comment", id: a.id, start: a.start, end: a.end, color: a.color, resolved: a.resolved };
      })
      .sort(function (a, b) { return a.start - b.start || b.end - a.end; });

    // comments win over revision marks when ranges overlap
    var clipped = [];
    var cursor = 0;
    comments.forEach(function (item) {
      var start = Math.max(item.start, cursor);
      if (start >= item.end) return;
      clipped.push(Object.assign({}, item, { start: start }));
      cursor = item.end;
    });

    var revisions = ((state.data.revisions || {})[sectionId] || []).map(function (r) {
      return { kind: "revision", start: r.start, end: r.end, color: r.color, label: r.label, author: r.author, when: r.when };
    }).sort(function (a, b) { return a.start - b.start; });

    var marks = [];
    var pos = 0;
    revisions.forEach(function (rev) {
      var start = rev.start, end = rev.end;
      // carve out space already used by comment highlights
      clipped.forEach(function (c) {
        if (end <= c.start || start >= c.end) return;
        if (start < c.start) marks.push(Object.assign({}, rev, { start: start, end: c.start }));
        start = Math.max(start, c.end);
      });
      if (start < end && end > pos) marks.push(Object.assign({}, rev, { start: start, end: end }));
      pos = Math.max(pos, end);
    });

    var all = clipped.concat(marks).sort(function (a, b) { return a.start - b.start; });
    // final safety pass: never overlap
    var safe = [];
    var limit = 0;
    all.forEach(function (m) {
      if (m.start < limit) m.start = limit;
      if (m.end > m.start) { safe.push(m); limit = m.end; }
    });
    return safe;
  }

  function buildDecorated(container, text, decorations) {
    var cursor = 0;
    decorations.forEach(function (m) {
      if (m.start > cursor) container.appendChild(document.createTextNode(text.slice(cursor, m.start)));
      var span = document.createElement("span");
      span.className = "ann" + (m.kind === "revision" ? " rev" : "") + (m.resolved ? " resolved" : "");
      span.style.setProperty("--c", m.color);
      span.textContent = text.slice(m.start, m.end);
      if (m.kind === "comment") {
        span.dataset.commentId = m.id;
        span.title = "Annotation #" + numberFor(m.id) + " — click to open";
      } else {
        span.dataset.revision = "1";
        span.title = m.label || ("Revised by " + m.author);
      }
      container.appendChild(span);
      cursor = m.end;
    });
    if (cursor < text.length) container.appendChild(document.createTextNode(text.slice(cursor)));
  }

  /* ---------------- render ---------------- */
  function render() {
    var p = state.data;
    app.innerHTML = "";

    var topbar = el("div", "topbar");
    topbar.appendChild(el("div", "doctitle", p.contract.title));
    var meta = el("div", "meta");
    meta.appendChild(el("span", "pill status", p.contract.status_label));
    var version = el("span", "pill", "v" + p.contract.version);
    meta.appendChild(version);
    var mine = el("span", "pill");
    var dot = el("span", "dot");
    dot.style.background = p.actor.color;
    dot.style.width = "10px"; dot.style.height = "10px";
    mine.appendChild(dot);
    mine.appendChild(document.createTextNode(p.actor.name + " · " + p.actor.role));
    meta.appendChild(mine);
    topbar.appendChild(meta);
    topbar.appendChild(el("div", "spacer"));
    if (!p.can_annotate) {
      var closed = el("span", "pill", "Review closed");
      topbar.appendChild(closed);
    }
    var printBtn = el("button", "btn", "Print / PDF");
    printBtn.addEventListener("click", function () { window.print(); });
    topbar.appendChild(printBtn);
    app.appendChild(topbar);

    var legend = el("div", "legend");
    (p.people || []).forEach(function (person) {
      var chip = el("span", "who");
      var swatch = el("span", "dot", initials(person.name));
      swatch.style.background = person.color;
      chip.appendChild(swatch);
      chip.appendChild(el("span", "nm", person.name));
      chip.appendChild(el("span", "role", "· " + person.role));
      legend.appendChild(chip);
    });
    app.appendChild(legend);

    var layout = el("div", "layout");
    var paper = el("main", "paper");
    paper.appendChild(el("h1", "", p.contract.title));
    var docmeta = el("div", "docmeta");
    docmeta.appendChild(el("span", "", "Status: " + p.contract.status_label));
    docmeta.appendChild(el("span", "", "Version v" + p.contract.version));
    docmeta.appendChild(el("span", "", "Reviewers: " + (p.people || []).length));
    docmeta.appendChild(el("span", "", p.can_annotate ? "Select any text to comment" : "Read-only"));
    paper.appendChild(docmeta);

    (p.sections || []).forEach(function (section, index) {
      paper.appendChild(renderSection(section, index));
    });

    var rail = renderRail();
    layout.appendChild(paper);
    layout.appendChild(rail);
    app.appendChild(layout);
    app.appendChild(renderPrintNotes());

    bindSelection();
    resize();

    // keep the open thread visible across Streamlit reruns
    if (state.openThreadId) {
      var stillThere = (p.annotations || []).filter(function (a) { return a.id === state.openThreadId; })[0];
      if (!stillThere) {
        state.openThreadId = null;
      } else {
        var anchor = document.querySelector('.ann[data-comment-id="' + state.openThreadId + '"]');
        if (anchor) openThread(state.openThreadId, anchor);
        else openPopover(state.openThreadId, { left: 320, right: 320, top: 160, width: 0, height: 0 });
      }
    }
  }

  function renderSection(section, index) {
    var wrap = el("section", "sec");
    wrap.dataset.sectionId = section.id;
    wrap.appendChild(el("h2", "", (index + 1) + ". " + section.title));
    var textEl = el("div", "text");
    textEl.dataset.sectionId = section.id;
    buildDecorated(textEl, section.content || "", decorationsFor(section.id));
    textEl.addEventListener("click", function (event) {
      var span = event.target.closest(".ann");
      if (span && span.dataset.commentId) {
        event.stopPropagation();
        openThread(span.dataset.commentId, span);
      }
    });
    wrap.appendChild(textEl);

    var changes = (state.data.changes || []).filter(function (c) {
      return c.section_id === section.id && c.status !== "MERGED";
    });
    if (changes.length) {
      var box = el("div", "changes");
      changes.forEach(function (change) { box.appendChild(renderChange(change)); });
      wrap.appendChild(box);
    }
    return wrap;
  }

  function renderChange(change) {
    var box = el("div", "change");
    var head = el("div", "chg-head");
    var badge = el("strong", "", "Tracked change · " + change.operation + " · " + change.status);
    head.appendChild(badge);
    var who = el("span", "", change.author + " · " + change.when);
    if (change.reviewed_by) who.textContent += " · reviewed by " + change.reviewed_by;
    head.appendChild(who);
    box.appendChild(head);

    var body = el("div", "chg-body");
    (change.diff || []).forEach(function (op) {
      if (op.op === "eq") body.appendChild(document.createTextNode(op.text));
      else {
        var span = el("span", op.op === "del" ? "del" : "ins", op.text);
        body.appendChild(span);
      }
    });
    box.appendChild(body);

    var canReview = state.data.can_review_changes && change.status === "PENDING";
    var canMerge = state.data.can_review_changes && change.status === "APPROVED";
    if (canReview || canMerge) {
      var actions = el("div", "chg-actions");
      if (canReview) {
        var accept = el("button", "btn primary", "Accept");
        accept.addEventListener("click", function () {
          emit({ action: "review_change", change_id: change.id, accept: true });
        });
        var reject = el("button", "btn", "Reject");
        reject.addEventListener("click", function () {
          emit({ action: "review_change", change_id: change.id, accept: false });
        });
        actions.appendChild(accept);
        actions.appendChild(reject);
      }
      if (canMerge) {
        var merge = el("button", "btn primary", "Merge into document");
        merge.addEventListener("click", function () {
          emit({ action: "merge_change", change_id: change.id });
        });
        actions.appendChild(merge);
      }
      box.appendChild(actions);
    }
    return box;
  }

  function renderRail() {
    var rail = el("aside", "rail");
    var head = el("div", "rail-head");
    head.appendChild(document.createTextNode("Comments"));
    var open = (state.data.annotations || []).filter(function (a) { return !a.resolved; }).length;
    head.appendChild(el("span", "count", String(open)));
    var spacer = el("span", "");
    spacer.style.flex = "1";
    head.appendChild(spacer);
    var toggle = el("label", "who");
    toggle.style.fontSize = "11px";
    toggle.style.color = "#4b5563";
    var cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = state.showResolved;
    cb.style.marginRight = "4px";
    cb.addEventListener("change", function () {
      state.showResolved = cb.checked;
      render();
      requestAnimationFrame(resize);
    });
    toggle.appendChild(cb);
    toggle.appendChild(document.createTextNode("resolved"));
    head.appendChild(toggle);
    rail.appendChild(head);

    var list = el("div", "rail-list");
    var items = (state.data.annotations || []).slice();
    if (!items.length) {
      list.appendChild(
        el("div", "rail-empty", state.data.can_annotate
          ? "Select a sentence in the document to add the first comment. Each reviewer annotates in their own colour."
          : "No annotations. The review window is closed.")
      );
    }
    items.forEach(function (a) {
      if (!state.showResolved && a.resolved) return;
      list.appendChild(renderCard(a));
    });
    rail.appendChild(list);
    return rail;
  }

  function renderCard(a) {
    var card = el("div", "card" + (a.resolved ? " resolved" : ""));
    card.style.setProperty("--c", a.color);
    card.dataset.commentId = a.id;
    var quote = el("div", "quote", "“" + (a.quote || "section note") + "”");
    card.appendChild(quote);
    var who = el("div", "who");
    var dot = el("span", "dot");
    dot.style.cssText = "width:14px;height:14px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;font-size:8px;color:#fff;background:" + a.color;
    dot.textContent = initials(a.author);
    who.appendChild(dot);
    who.appendChild(el("span", "nm", a.author));
    who.appendChild(el("span", "when", a.when + (a.resolved ? " · resolved" : "")));
    card.appendChild(who);
    card.appendChild(el("div", "body", a.text));
    if (a.replies && a.replies.length) {
      var replies = el("div", "replies");
      a.replies.forEach(function (r) {
        var row = el("div", "r");
        var b = el("b", "", r.author + ": ");
        row.appendChild(b);
        row.appendChild(document.createTextNode(r.text));
        replies.appendChild(row);
      });
      card.appendChild(replies);
    }
    card.addEventListener("click", function (event) {
      event.stopPropagation();
      var target = document.querySelector('.ann[data-comment-id="' + a.id + '"]');
      if (target) {
        target.scrollIntoView({ block: "center", behavior: "smooth" });
        openThread(a.id, target);
      } else {
        openPopover(a.id, { left: window.innerWidth - 360, top: 140, width: 0, height: 0 });
      }
    });
    return card;
  }

  function renderPrintNotes() {
    var box = el("div", "print-only");
    box.appendChild(el("h3", "", "Comments"));
    (state.data.annotations || []).forEach(function (a) {
      var note = el("div", "pn");
      var b = el("b", "", numberFor(a.id) + ". " + a.author);
      note.appendChild(b);
      note.appendChild(document.createTextNode(
        " · " + a.when + (a.resolved ? " · resolved" : "") + " — “" + (a.quote || "") + "” — " + a.text
      ));
      (a.replies || []).forEach(function (r) {
        var reply = el("div", "pn");
        reply.style.marginLeft = "18px";
        reply.textContent = "↳ " + r.author + " · " + r.when + ": " + r.text;
        note.appendChild(reply);
      });
      box.appendChild(note);
    });
    return box;
  }

  /* ---------------- selection ---------------- */
  function offsetIn(container, node, offset) {
    var walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT, null);
    var total = 0;
    var current;
    while ((current = walker.nextNode())) {
      if (current === node) return total + offset;
      total += current.nodeValue.length;
    }
    return total;
  }

  function bindSelection() {
    var paper = document.querySelector(".paper");
    if (!paper) return;
    paper.addEventListener("mouseup", function () {
      if (!state.data.can_annotate) return;
      setTimeout(captureSelection, 0);
    });
    document.addEventListener("mousedown", function (event) {
      if (!event.target.closest(".selbar") && !event.target.closest(".ann")) hideSelbar();
      if (!event.target.closest(".popover") && !event.target.closest(".ann") && !event.target.closest(".card")) {
        hidePopover();
      }
    });
  }

  function captureSelection() {
    var sel = window.getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount) { hideSelbar(); return; }
    var range = sel.getRangeAt(0);
    var node = range.commonAncestorContainer;
    var host = node.nodeType === 1 ? node : node.parentElement;
    var textEl = host ? host.closest(".text") : null;
    if (!textEl) { hideSelbar(); return; }
    if (!textEl.contains(range.startContainer) || !textEl.contains(range.endContainer)) { hideSelbar(); return; }
    var start = offsetIn(textEl, range.startContainer, range.startOffset);
    var end = offsetIn(textEl, range.endContainer, range.endOffset);
    if (end < start) { var swap = start; start = end; end = swap; }
    if (end - start < 1) { hideSelbar(); return; }
    var section = sectionById(textEl.dataset.sectionId);
    if (!section) { hideSelbar(); return; }
    var rect = range.getBoundingClientRect();
    state.selection = {
      sectionId: section.id,
      start: start,
      end: end,
      quote: (section.content || "").slice(start, end),
      rect: rect
    };
    state.selMode = "comment";
    showSelbar();
  }

  function showSelbar() {
    var bar = document.querySelector(".selbar");
    if (!bar) {
      bar = el("div", "selbar");
      document.body.appendChild(bar);
    }
    bar.innerHTML = "";
    var mode = state.selMode;
    var colors = (state.data.palette || []).map(function (c) { return c.color; });
    if (!state.activeColor) state.activeColor = state.data.actor.color;

    var row = el("div", "row");
    colors.forEach(function (color) {
      var sw = document.createElement("button");
      sw.className = "sw" + (color === state.activeColor ? " active" : "");
      sw.style.background = color;
      sw.title = "Annotate in this colour";
      sw.addEventListener("click", function (event) {
        event.stopPropagation();
        state.activeColor = color;
        showSelbar();
      });
      row.appendChild(sw);
    });
    bar.appendChild(row);

    var quote = el("div", "quote", "“" + state.selection.quote.slice(0, 160) + (state.selection.quote.length > 160 ? "…" : "") + "”");
    bar.appendChild(quote);

    var ta = document.createElement("textarea");
    ta.placeholder = mode === "comment" ? "Write your comment…" : "Replace the selected text (leave empty to delete it)";
    if (mode === "change") ta.value = state.selection.quote;
    bar.appendChild(ta);

    var actions = el("div", "actions");
    var cancel = el("button", "btn", "Cancel");
    cancel.addEventListener("click", function (event) {
      event.stopPropagation();
      hideSelbar();
      var s = window.getSelection();
      if (s) s.removeAllRanges();
    });
    actions.appendChild(cancel);

    if (state.data.can_propose && mode === "comment") {
      var suggest = el("button", "btn", "Suggest edit");
      suggest.addEventListener("click", function (event) {
        event.stopPropagation();
        state.selMode = "change";
        showSelbar();
      });
      actions.appendChild(suggest);
    }
    if (mode === "change" && state.data.can_propose) {
      var back = el("button", "btn", "Back");
      back.addEventListener("click", function (event) {
        event.stopPropagation();
        state.selMode = "comment";
        showSelbar();
      });
      actions.appendChild(back);
    }

    var submit = el("button", "btn primary", mode === "comment" ? "Add comment" : "Submit change");
    submit.addEventListener("click", function (event) {
      event.stopPropagation();
      var value = ta.value.trim();
      var sel = state.selection;
      if (mode === "comment") {
        if (!value) { ta.focus(); return; }
        emit({
          action: "comment",
          section_id: sel.sectionId,
          start: sel.start,
          end: sel.end,
          quote: sel.quote,
          message: value,
          color: state.activeColor
        });
      } else {
        var section = sectionById(sel.sectionId);
        if (!section) return;
        var after = section.content.slice(0, sel.start) + ta.value + section.content.slice(sel.end);
        if (after === section.content) { ta.focus(); return; }
        emit({
          action: "propose_change",
          section_id: sel.sectionId,
          operation: ta.value.trim() ? "UPDATE" : "DELETE",
          before_content: section.content,
          after_content: after
        });
      }
      hideSelbar();
      var s2 = window.getSelection();
      if (s2) s2.removeAllRanges();
    });
    actions.appendChild(submit);
    bar.appendChild(actions);

    var hint = el("div", "hint", mode === "comment"
      ? "Your comment is anchored to the selected words"
      : "Reviewers will see this as a tracked change");
    bar.appendChild(hint);

    bar.classList.add("open");
    var rect = state.selection.rect;
    var left = Math.min(Math.max(rect.left + rect.width / 2, 150), window.innerWidth - 150);
    bar.style.left = left + "px";
    bar.style.top = Math.max(rect.top - 10, 8) + "px";
    setTimeout(function () { ta.focus(); }, 30);
    requestAnimationFrame(resize);
  }

  function hideSelbar() {
    var bar = document.querySelector(".selbar");
    if (bar) bar.classList.remove("open");
    state.selection = null;
    state.selMode = "comment";
  }

  /* ---------------- thread popover ---------------- */
  function openThread(id, anchorEl) {
    var rect = anchorEl.getBoundingClientRect();
    openPopover(id, rect);
    markActiveCard(id);
  }

  function openPopover(id, rect) {
    var annotation = (state.data.annotations || []).filter(function (a) { return a.id === id; })[0];
    if (!annotation) return;
    state.openThreadId = id;

    var pop = document.querySelector(".popover");
    if (!pop) {
      pop = el("div", "popover");
      document.body.appendChild(pop);
    }
    pop.innerHTML = "";
    pop.style.setProperty("--c", annotation.color);

    var head = el("div", "p-head");
    var dot = el("span", "dot");
    dot.style.cssText = "width:16px;height:16px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;font-size:8px;color:#fff;background:" + annotation.color;
    dot.textContent = initials(annotation.author);
    head.appendChild(dot);
    head.appendChild(el("span", "nm", annotation.author));
    head.appendChild(el("span", "", "· " + annotation.author_role));
    head.appendChild(el("span", "", "· " + annotation.when));
    if (annotation.resolved) head.appendChild(el("span", "pill", "resolved"));
    var close = el("button", "btn subtle", "✕");
    close.style.marginLeft = "auto";
    close.addEventListener("click", function (event) { event.stopPropagation(); hidePopover(); });
    head.appendChild(close);
    pop.appendChild(head);

    if (annotation.quote) pop.appendChild(el("div", "p-quote", "“" + annotation.quote + "”"));
    pop.appendChild(el("div", "p-body", annotation.text));

    (annotation.replies || []).forEach(function (reply) {
      var row = el("div", "p-reply");
      var b = el("b", "", reply.author);
      row.appendChild(b);
      row.appendChild(document.createTextNode(" · " + reply.when));
      row.appendChild(el("div", "", reply.text));
      pop.appendChild(row);
    });

    if (state.data.can_annotate) {
      var ta = document.createElement("textarea");
      ta.placeholder = "Reply…";
      pop.appendChild(ta);
      var actions = el("div", "p-actions");
      var sendBtn = el("button", "btn primary", "Reply");
      sendBtn.addEventListener("click", function (event) {
        event.stopPropagation();
        var text = ta.value.trim();
        if (!text) { ta.focus(); return; }
        // keep the popover open: the reply shows up as soon as Streamlit reruns
        sendBtn.textContent = "Sending…";
        emit({ action: "reply", comment_id: annotation.id, message: text, color: state.activeColor });
      });
      actions.appendChild(sendBtn);

      var resolveBtn = el("button", "btn", annotation.resolved ? "Reopen" : "Resolve");
      resolveBtn.addEventListener("click", function (event) {
        event.stopPropagation();
        emit({ action: "resolve", comment_id: annotation.id, resolved: !annotation.resolved });
        hidePopover();
      });
      actions.appendChild(resolveBtn);

      if (annotation.own) {
        var del = el("button", "btn danger", "Delete");
        del.addEventListener("click", function (event) {
          event.stopPropagation();
          emit({ action: "delete_comment", comment_id: annotation.id });
          hidePopover();
        });
        actions.appendChild(del);
      }
      pop.appendChild(actions);
    }

    pop.classList.add("open");
    var width = 320;
    var left = rect.right + 12;
    if (left + width > window.innerWidth - 8) left = Math.max(8, rect.left - width - 12);
    var top = Math.min(Math.max(rect.top - 6, 60), window.innerHeight - 320);
    pop.style.left = left + "px";
    pop.style.top = top + "px";
    requestAnimationFrame(resize);
  }

  function hidePopover() {
    var pop = document.querySelector(".popover");
    if (pop) pop.classList.remove("open");
    state.openThreadId = null;
    markActiveCard(null);
  }

  function markActiveCard(id) {
    Array.prototype.forEach.call(document.querySelectorAll(".card"), function (card) {
      if (id && card.dataset.commentId === id) card.classList.add("active");
      else card.classList.remove("active");
    });
  }

  /* ---------------- boot ---------------- */
  send("streamlit:componentReady", { apiVersion: 1 });
  resize();
  if (window.ResizeObserver) {
    new ResizeObserver(function () { resize(); }).observe(app);
  }
  window.addEventListener("resize", function () { resize(); });
})();
