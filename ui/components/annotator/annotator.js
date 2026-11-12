/* Document review + simple word processor for the contract MVP.
 *
 * Streamlit custom component (no build step). Two modes share one paper:
 *
 *   Review — the document rendered from the block model; select words to
 *            comment or suggest an edit; highlights keep the reviewer colour.
 *   Edit   — the same blocks wrapped in a contenteditable surface so whole
 *            paragraphs can be cut, pasted, reordered or deleted. A toolbar
 *            covers headings, lists, tables and inline styling; every action
 *            works with the browser's native selection and undo stack.
 *
 * On save the DOM is serialised back to the Markdown subset parsed by
 * core/richtext.py, so the stored clause stays plain text and every downstream
 * feature (diff, audit, anchors, PDF) keeps working.
 */
(function () {
  "use strict";

  var app = document.getElementById("app");
  var state = {
    data: null,
    mode: "view",
    activeColor: null,
    selection: null,
    selMode: "comment",
    openThreadId: null,
    showResolved: true,
    lineNumbers: false,
    dirty: false,
    focusSection: null,
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
    // Never wipe a dirty editor: Streamlit re-sends the payload whenever the
    // frame is re-measured, which used to discard unsaved typing mid-keystroke.
    if (state.mode === "edit" && state.dirty) {
      state.pendingPayload = args.payload;
      return;
    }
    state.data = args.payload;
    state.dirty = false;
    if (state.mode === "edit" && !canEdit()) state.mode = "view";
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

  function canEdit() {
    return !!(state.data && (state.data.can_edit || state.data.can_propose));
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
      return { kind: "revision", start: r.start, end: r.end, color: r.color, label: r.label, author: r.author };
    }).sort(function (a, b) { return a.start - b.start; });

    var marks = [];
    var position = 0;
    revisions.forEach(function (rev) {
      var start = rev.start, end = rev.end;
      clipped.forEach(function (c) {
        if (end <= c.start || start >= c.end) return;
        if (start < c.start) marks.push(Object.assign({}, rev, { start: start, end: c.start }));
        start = Math.max(start, c.end);
      });
      if (start < end && end > position) marks.push(Object.assign({}, rev, { start: start, end: end }));
      position = Math.max(position, end);
    });

    var all = clipped.concat(marks).sort(function (a, b) { return a.start - b.start; });
    var safe = [];
    var limit = 0;
    all.forEach(function (m) {
      if (m.start < limit) m.start = limit;
      if (m.end > m.start) { safe.push(m); limit = m.end; }
    });
    return safe;
  }

  /* ---------------- inline rendering ---------------- */
  function wrap(tag, child) {
    var node = document.createElement(tag);
    node.appendChild(child);
    return node;
  }

  function buildPiece(text, run, dec) {
    var node = document.createTextNode(text);
    if (run.code) node = wrap("code", node);
    if (run.bold) node = wrap("strong", node);
    if (run.italic) node = wrap("em", node);
    if (run.underline) node = wrap("u", node);
    if (run.strike) node = wrap("del", node);
    if (dec) {
      var span = el(
        "span",
        "ann" + (dec.kind === "revision" ? " rev" : "") + (dec.resolved ? " resolved" : "")
      );
      span.style.setProperty("--c", dec.color);
      if (dec.kind === "revision") {
        span.dataset.revision = "1";
        span.title = dec.label || "";
      } else {
        span.dataset.commentId = dec.id;
        span.title = "Annotation #" + numberFor(dec.id) + " — click to open";
      }
      span.appendChild(node);
      node = span;
    }
    return node;
  }

  function segments(text, abs, decs) {
    if (!decs || !decs.length) return [{ text: text, dec: null }];
    var cuts = [0, text.length];
    decs.forEach(function (dec) {
      var start = Math.max(dec.start, abs) - abs;
      var end = Math.min(dec.end, abs + text.length) - abs;
      if (start < end) cuts.push(start, end);
    });
    cuts.sort(function (a, b) { return a - b; });
    var out = [];
    for (var i = 1; i < cuts.length; i++) {
      if (cuts[i] <= cuts[i - 1]) continue;
      var from = cuts[i - 1], to = cuts[i];
      var middle = abs + from;
      var hit = null;
      decs.forEach(function (dec) {
        if (!hit && dec.start <= middle && dec.end >= abs + to) hit = dec;
      });
      out.push({ text: text.slice(from, to), dec: hit });
    }
    return out.length ? out : [{ text: text, dec: null }];
  }

  function renderInline(target, runs, base, decs) {
    var painted = false;
    (runs || []).forEach(function (run) {
      segments(run.text, base + run.p, decs).forEach(function (piece) {
        if (!piece.text) return;
        target.appendChild(buildPiece(piece.text, run, piece.dec));
        painted = true;
      });
    });
    if (!painted) target.appendChild(document.createTextNode(""));
  }

  /* ---------------- block rendering ---------------- */
  function renderBlock(block, decs) {
    var node;
    if (block.type === "h1" || block.type === "h2" || block.type === "h3") {
      node = el(block.type.toUpperCase(), "blk " + block.type);
      node.dataset.start = block.start;
      node.dataset.linehost = "1";
      renderInline(node, block.runs, block.start, decs);
      return node;
    }
    if (block.type === "ul" || block.type === "ol") {
      node = el(block.type === "ol" ? "OL" : "UL", "blk list");
      (block.items || []).forEach(function (item) {
        var li = el("li");
        li.dataset.start = block.start + item.start;
        li.dataset.linehost = "1";
        renderInline(li, item.runs, block.start + item.start, decs);
        node.appendChild(li);
      });
      return node;
    }
    if (block.type === "table") {
      node = el("table", "blk-table");
      var body = el("tbody");
      (block.rows || []).forEach(function (row) {
        var tr = el("tr");
        tr.dataset.linehost = "1";
        (row.cells || []).forEach(function (cell) {
          var td = el(row.header ? "th" : "td");
          td.dataset.start = block.start + cell.start;
          renderInline(td, cell.runs, block.start + cell.start, decs);
          tr.appendChild(td);
        });
        body.appendChild(tr);
      });
      node.appendChild(body);
      return node;
    }
    if (block.type === "quote") {
      node = el("blockquote", "blk");
      node.dataset.start = block.start;
      node.dataset.linehost = "1";
      renderInline(node, block.runs, block.start, decs);
      return node;
    }
    if (block.type === "hr") return el("hr");
    node = el("p", "blk");
    node.dataset.start = block.start;
    node.dataset.linehost = "1";
    renderInline(node, block.runs, block.start, decs);
    return node;
  }

  /* ---------------- render ---------------- */
  function render() {
    var p = state.data;
    app.innerHTML = "";

    app.appendChild(renderTopbar());
    if (state.mode === "edit") app.appendChild(renderToolbar());
    app.appendChild(renderLegend());

    var layout = el("div", "layout");
    var paper = el("main", "paper" + (state.lineNumbers ? " with-lines" : ""));
    paper.appendChild(el("h1", "doc-title", p.contract.title));
    var docmeta = el("div", "docmeta");
    docmeta.appendChild(el("span", "", "Status: " + p.contract.status_label));
    docmeta.appendChild(el("span", "", "Version v" + p.contract.version));
    docmeta.appendChild(el("span", "", "Reviewers: " + (p.people || []).length));
    docmeta.appendChild(
      el("span", "", state.mode === "edit" ? "Editing — save to apply" : (p.can_annotate ? "Select any text to comment" : "Read-only"))
    );
    paper.appendChild(docmeta);

    (p.sections || []).forEach(function (section, index) {
      paper.appendChild(renderSection(section, index));
    });
    paper.appendChild(el("div", "gutter"));

    layout.appendChild(paper);
    layout.appendChild(renderRail());
    app.appendChild(layout);
    app.appendChild(renderPrintNotes());

    if (state.mode === "view") bindSelection();
    paintLineNumbers();
    resize();

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

  function renderTopbar() {
    var p = state.data;
    var bar = el("div", "topbar");
    bar.appendChild(el("div", "doctitle", p.contract.title));
    var meta = el("div", "meta");
    meta.appendChild(el("span", "pill status", p.contract.status_label));
    meta.appendChild(el("span", "pill", "v" + p.contract.version));
    var mine = el("span", "pill");
    var dot = el("span", "dot");
    dot.style.cssText = "width:10px;height:10px;border-radius:50%;display:inline-block;background:" + p.actor.color;
    mine.appendChild(dot);
    var mineRole = p.actor.side_label && p.actor.side ? p.actor.side_label : (p.actor.role || "");
    mine.appendChild(document.createTextNode(p.actor.name + (mineRole ? " · " + mineRole : "")));
    meta.appendChild(mine);
    bar.appendChild(meta);
    bar.appendChild(el("div", "spacer"));

    if (canEdit()) {
      var modes = el("div", "modes");
      modes.appendChild(
        modeButton("Review", "view", "Read, comment and track changes")
      );
      modes.appendChild(
        modeButton("Edit (tracked)", "edit", "Edit the document; changes are submitted as tracked changes for the opposite party")
      );
      bar.appendChild(modes);
    }
    if (!p.can_annotate) bar.appendChild(el("span", "pill", "Review closed"));

    var lines = el("button", "btn" + (state.lineNumbers ? " active" : ""), "123");
    lines.title = "Show line numbers";
    lines.addEventListener("click", function () {
      state.lineNumbers = !state.lineNumbers;
      var paper = document.querySelector(".paper");
      if (paper) paper.classList.toggle("with-lines", state.lineNumbers);
      this.classList.toggle("active", state.lineNumbers);
      paintLineNumbers();
      resize();
    });
    bar.appendChild(lines);

    var printBtn = el("button", "btn", "Print / PDF");
    printBtn.addEventListener("click", function () { window.print(); });
    bar.appendChild(printBtn);
    return bar;
  }

  function modeButton(label, mode, hint) {
    var btn = el("button", "mode-btn" + (state.mode === mode ? " active" : ""), label);
    btn.title = hint;
    btn.addEventListener("click", function () { setMode(mode); });
    return btn;
  }

  function setMode(mode) {
    if (mode === state.mode) return;
    if (mode === "view" && state.dirty && !window.confirm("Discard the unsaved edits?")) return;
    state.mode = mode;
    state.dirty = false;
    if (state.pendingPayload) {
      state.data = state.pendingPayload;
      state.pendingPayload = null;
    }
    render();
    requestAnimationFrame(resize);
  }

  function renderLegend() {
    var legend = el("div", "legend");
    (state.data.people || []).forEach(function (person) {
      var chip = el("span", "who");
      var swatch = el("span", "dot", initials(person.name));
      swatch.style.background = person.color;
      chip.appendChild(swatch);
      chip.appendChild(el("span", "nm", person.name));
      var personRole = person.side_label && person.side ? person.side_label : (person.role || "");
      chip.appendChild(el("span", "role", "· " + personRole));
      legend.appendChild(chip);
    });
    return legend;
  }

  function renderSection(section, index) {
    var wrap = el("section", "sec");
    wrap.dataset.sectionId = section.id;

    var head = el("div", "sec-head");
    head.appendChild(el("span", "sec-no", String(index + 1) + "."));
    if (state.mode === "edit" && state.data.can_edit) {
      var input = document.createElement("input");
      input.className = "sec-title-input";
      input.value = section.title;
      input.setAttribute("aria-label", "Section title");
      input.addEventListener("change", function () {
        if (input.value.trim() && input.value.trim() !== section.title) {
          structural({ action: "rename_section", section_id: section.id, title: input.value.trim() });
        }
      });
      head.appendChild(input);
      var tools = el("div", "sec-tools");
      tools.appendChild(toolBtn("↑", "Move section up", function () {
        structural({ action: "move_section", section_id: section.id, direction: -1 });
      }));
      tools.appendChild(toolBtn("↓", "Move section down", function () {
        structural({ action: "move_section", section_id: section.id, direction: 1 });
      }));
      tools.appendChild(toolBtn("＋", "Add a section below", function () {
        structural({ action: "add_section", after_section_id: section.id });
      }));
      tools.appendChild(toolBtn("🗑", "Delete this section", function () {
        if (window.confirm("Delete section “" + section.title + "”?")) {
          structural({ action: "delete_section", section_id: section.id });
        }
      }));
      head.appendChild(tools);
    } else {
      head.appendChild(el("h2", "sec-title", section.title));
    }
    wrap.appendChild(head);

    var body = el("div", state.mode === "edit" ? "body editable" : "body");
    body.dataset.sectionId = section.id;
    var decs = state.mode === "edit" ? [] : decorationsFor(section.id);

    if (!(section.blocks || []).length) {
      var empty = el("p", "blk");
      empty.dataset.start = 0;
      empty.dataset.linehost = "1";
      empty.appendChild(document.createElement("br"));
      body.appendChild(empty);
    }
    (section.blocks || []).forEach(function (block) {
      body.appendChild(renderBlock(block, decs));
    });

    if (state.mode === "edit") {
      body.contentEditable = "true";
      body.spellcheck = false;
      body.addEventListener("focusin", function () { state.focusSection = section.id; });
      body.addEventListener("input", afterEdit);
      body.addEventListener("paste", onPaste);
      body.addEventListener("keyup", scheduleLines);
      body.addEventListener("mouseup", scheduleLines);
    }
    wrap.appendChild(body);

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
    head.appendChild(el("strong", "", "Tracked change · " + change.operation + " · " + change.status));
    var who = el("span", "", change.author + " · " + change.when);
    if (change.reviewed_by) who.textContent += " · reviewed by " + change.reviewed_by;
    head.appendChild(who);
    box.appendChild(head);

    var body = el("div", "chg-body");
    (change.diff || []).forEach(function (op) {
      if (op.op === "eq") body.appendChild(document.createTextNode(op.text));
      else body.appendChild(el("span", op.op === "del" ? "del" : "ins", op.text));
    });
    box.appendChild(body);

    var canReview = change.can_review;
    var canMerge = change.can_merge;
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

  /* ---------------- toolbar (edit mode) ---------------- */
  function toolBtn(label, hint, handler, cls) {
    var btn = el("button", "tb-btn" + (cls ? " " + cls : ""), label);
    btn.type = "button";
    btn.title = hint;
    // keep the caret where it is: never let the button take focus
    btn.addEventListener("mousedown", function (event) { event.preventDefault(); });
    btn.addEventListener("click", function (event) {
      event.preventDefault();
      handler();
    });
    return btn;
  }

  function renderToolbar() {
    var bar = el("div", "toolbar");

    var select = document.createElement("select");
    select.className = "tb-select";
    select.title = "Paragraph style";
    [["p", "Body text"], ["h1", "Title"], ["h2", "Heading 1"], ["h3", "Heading 2"], ["blockquote", "Quote"]].forEach(
      function (option) {
        var node = document.createElement("option");
        node.value = option[0];
        node.textContent = option[1];
        select.appendChild(node);
      }
    );
    select.addEventListener("mousedown", function () { state.focusSection && focusEditable(); });
    select.addEventListener("change", function () {
      applyBlock(select.value);
      select.selectedIndex = 0;
    });
    bar.appendChild(select);

    bar.appendChild(toolBtn("B", "Bold (Ctrl+B)", function () { exec("bold"); }, "b"));
    bar.appendChild(toolBtn("I", "Italic (Ctrl+I)", function () { exec("italic"); }, "i"));
    bar.appendChild(toolBtn("U", "Underline (Ctrl+U)", function () { exec("underline"); }, "u"));
    bar.appendChild(toolBtn("S", "Strikethrough", function () { exec("strikeThrough"); }, "s"));
    bar.appendChild(el("span", "tb-sep"));
    bar.appendChild(toolBtn("• List", "Bulleted list", function () { exec("insertUnorderedList"); }));
    bar.appendChild(toolBtn("1. List", "Numbered list", function () { exec("insertOrderedList"); }));
    bar.appendChild(toolBtn("▦ Table", "Insert a table", function (event) { openTablePicker(event); }));
    bar.appendChild(toolBtn("— Line", "Horizontal rule", function () { exec("insertHorizontalRule"); }));
    bar.appendChild(el("span", "tb-sep"));
    bar.appendChild(toolBtn("⇤", "Clear formatting", function () { exec("removeFormat"); }));
    bar.appendChild(toolBtn("↶", "Undo (Ctrl+Z)", function () { exec("undo"); }));
    bar.appendChild(toolBtn("↷", "Redo (Ctrl+Shift+Z)", function () { exec("redo"); }));

    bar.appendChild(el("span", "spacer"));

    var discard = el("button", "btn", "Discard");
    discard.addEventListener("click", function () {
      if (!state.dirty || window.confirm("Discard the unsaved edits?")) {
        state.dirty = false;
        render();
        requestAnimationFrame(resize);
      }
    });
    bar.appendChild(discard);

    var save = el("button", "btn primary", "Save changes");
    save.id = "save-btn";
    save.title = "Submit your edits as tracked changes for the opposite party to accept and merge";
    save.disabled = !state.dirty;
    save.addEventListener("click", saveEdits);
    bar.appendChild(save);
    return bar;
  }

  function refreshSaveBtn() {
    var btn = document.getElementById("save-btn");
    if (btn) btn.disabled = !state.dirty;
  }

  function exec(command, value) {
    if (!focusEditable()) return;
    document.execCommand(command, false, value === undefined ? null : value);
    afterEdit();
  }

  function applyBlock(tag) {
    if (!focusEditable()) return;
    document.execCommand("formatBlock", false, tag === "p" ? "p" : tag);
    afterEdit();
  }

  function focusEditable() {
    var sel = window.getSelection();
    var node = sel && sel.rangeCount ? sel.getRangeAt(0).commonAncestorContainer : null;
    var host = node ? (node.nodeType === 1 ? node : node.parentElement) : null;
    var editable = host ? host.closest(".editable") : null;
    if (!editable && state.focusSection) {
      editable = document.querySelector('.editable[data-section-id="' + state.focusSection + '"]');
    }
    if (!editable) return null;
    if (sel && sel.rangeCount && editable.contains(sel.getRangeAt(0).commonAncestorContainer)) return editable;
    editable.focus();
    var range = document.createRange();
    range.selectNodeContents(editable);
    range.collapse(false);
    sel.removeAllRanges();
    sel.addRange(range);
    return editable;
  }

  function onPaste(event) {
    event.preventDefault();
    var text = "";
    try {
      text = (event.clipboardData || window.clipboardData).getData("text/plain") || "";
    } catch (err) {
      text = "";
    }
    if (!text) return;
    var html = text.split(/\r?\n/).map(function (line) {
      return line.replace(/[&<>]/g, function (ch) {
        return ch === "&" ? "&amp;" : ch === "<" ? "&lt;" : "&gt;";
      });
    }).join("<br>");
    document.execCommand("insertHTML", false, html);
    afterEdit();
  }

  function openTablePicker() {
    var existing = document.querySelector(".tbl-picker");
    if (existing) { existing.remove(); return; }
    var host = focusEditable();
    if (!host) return;
    var picker = el("div", "tbl-picker");
    var label = el("div", "tp-label", "Insert table");
    picker.appendChild(label);
    var grid = el("div", "tp-grid");
    for (var r = 1; r <= 6; r++) {
      for (var c = 1; c <= 6; c++) {
        (function (rows, cols) {
          var cell = el("div", "tp-cell");
          cell.dataset.rows = String(rows);
          cell.dataset.cols = String(cols);
          cell.addEventListener("mouseenter", function () {
            label.textContent = rows + " × " + cols;
            Array.prototype.forEach.call(grid.children, function (node) {
              var on = Number(node.dataset.rows) <= rows && Number(node.dataset.cols) <= cols;
              node.classList.toggle("on", on);
            });
          });
          cell.addEventListener("mousedown", function (event) { event.preventDefault(); });
          cell.addEventListener("click", function (event) {
            event.preventDefault();
            insertTable(rows, cols);
            picker.remove();
          });
          grid.appendChild(cell);
        })(r, c);
      }
    }
    picker.appendChild(grid);
    var rect = host.getBoundingClientRect();
    picker.style.left = Math.min(rect.left + 40, window.innerWidth - 220) + "px";
    picker.style.top = window.scrollY + 96 + "px";
    document.body.appendChild(picker);
    setTimeout(function () {
      document.addEventListener("mousedown", closePicker, { once: true });
    }, 0);
    function closePicker(event) {
      if (!event.target.closest(".tbl-picker")) picker.remove();
    }
  }

  function insertTable(rows, cols) {
    if (!focusEditable()) return;
    var html = '<table class="blk-table"><tbody>';
    for (var r = 0; r < rows; r++) {
      html += "<tr>";
      for (var c = 0; c < cols; c++) {
        html += r === 0 ? "<th>Column " + (c + 1) + "</th>" : "<td><br></td>";
      }
      html += "</tr>";
    }
    html += '</tbody></table><p><br></p>';
    document.execCommand("insertHTML", false, html);
    afterEdit();
  }

  function afterEdit() {
    state.dirty = true;
    refreshSaveBtn();
    refreshLineHosts();
    scheduleLines();
    scheduleResize();
  }

  /* execCommand turns a paragraph into <h2>, wraps lines in <div> and inserts
   * tables without our data attributes; line numbering needs them back. */
  function refreshLineHosts() {
    Array.prototype.forEach.call(document.querySelectorAll(".editable"), function (host) {
      Array.prototype.forEach.call(host.children, function (child) {
        if (child.tagName === "TABLE") {
          Array.prototype.forEach.call(child.querySelectorAll("tr"), function (row) {
            row.dataset.linehost = "1";
          });
        } else if (child.tagName !== "HR") {
          child.dataset.linehost = "1";
        }
      });
    });
  }

  var lineTimer = null;
  function scheduleLines() {
    if (lineTimer) clearTimeout(lineTimer);
    lineTimer = setTimeout(function () { paintLineNumbers(); }, 160);
  }

  var resizeTimer = null;
  function scheduleResize() {
    if (resizeTimer) clearTimeout(resizeTimer);
    resizeTimer = setTimeout(resize, 200);
  }

  /* ---------------- DOM → Markdown ---------------- */
  function escMd(text) {
    return String(text).replace(/([\\`*_+~|])/g, "\\$1");
  }

  function inlineMd(node) {
    var out = "";
    Array.prototype.forEach.call(node.childNodes, function (child) {
      if (child.nodeType === 3) { out += escMd(child.nodeValue); return; }
      if (child.nodeType !== 1) return;
      var tag = child.tagName;
      if (tag === "BR") { out += "\n"; return; }
      var inner = inlineMd(child);
      if (!inner) return;
      if (tag === "B" || tag === "STRONG") out += "**" + inner + "**";
      else if (tag === "I" || tag === "EM") out += "*" + inner + "*";
      else if (tag === "U") out += "++" + inner + "++";
      else if (tag === "S" || tag === "STRIKE" || tag === "DEL") out += "~~" + inner + "~~";
      else if (tag === "CODE") out += "`" + inner + "`";
      else if (tag === "SPAN") {
        var computed = window.getComputedStyle(child);
        var weight = parseInt(computed.fontWeight, 10) || 400;
        var deco = computed.textDecorationLine || computed.textDecoration || "";
        if (weight >= 600) inner = "**" + inner + "**";
        if (/underline/.test(deco)) inner = "++" + inner + "++";
        if (/line-through/.test(deco)) inner = "~~" + inner + "~~";
        out += inner;
      } else if (tag === "DIV" || tag === "P") {
        out += (/[\n]$/.test(out) || !out ? "" : "\n") + inner;
      } else {
        out += inner;
      }
    });
    return out;
  }

  function serializeSection(host) {
    var parts = [];
    var buffer = "";
    function flush() {
      var text = buffer.trim();
      if (text) parts.push(text);
      buffer = "";
    }
    Array.prototype.forEach.call(host.childNodes, function (node) {
      if (node.nodeType === 3) { buffer += escMd(node.nodeValue); return; }
      if (node.nodeType !== 1) return;
      var tag = node.tagName;
      if (tag === "BR") { buffer += "\n"; return; }
      flush();
      if (tag === "H1" || tag === "H2" || tag === "H3") {
        var heading = inlineMd(node).trim();
        if (heading) parts.push("###".slice(0, Number(tag[1])) + " " + heading);
      } else if (tag === "UL" || tag === "OL") {
        var ordered = tag === "OL";
        var counter = 1;
        var items = [];
        Array.prototype.forEach.call(node.children, function (li) {
          if (li.tagName !== "LI") return;
          var text = inlineMd(li).replace(/\s*\n\s*/g, " ").trim();
          if (!text) return;
          items.push((ordered ? counter++ + ". " : "- ") + text);
        });
        if (items.length) parts.push(items.join("\n"));
      } else if (tag === "BLOCKQUOTE") {
        var quote = inlineMd(node).trim();
        if (quote) parts.push(quote.split("\n").map(function (line) { return "> " + line; }).join("\n"));
      } else if (tag === "TABLE") {
        var rows = [];
        Array.prototype.forEach.call(node.querySelectorAll("tr"), function (tr) {
          var cells = [];
          Array.prototype.forEach.call(tr.children, function (cell) {
            cells.push(inlineMd(cell).replace(/\s*\n\s*/g, " ").trim());
          });
          if (!cells.length) return;
          if (!rows.length) {
            rows.push("| " + cells.join(" | ") + " |");
            rows.push("| " + cells.map(function () { return "---"; }).join(" | ") + " |");
          } else {
            rows.push("| " + cells.join(" | ") + " |");
          }
        });
        if (rows.length) parts.push(rows.join("\n"));
      } else if (tag === "HR") {
        parts.push("---");
      } else {
        var text = inlineMd(node);
        if (text.trim()) parts.push(text);
      }
    });
    flush();
    return parts.join("\n\n");
  }

  function structural(action) {
    // structural changes rerun Streamlit, which would throw away an unsaved buffer
    if (state.dirty && !window.confirm("You have unsaved edits. Discard them and continue?")) return;
    state.dirty = false;
    state.pendingPayload = null;
    emit(action);
  }

  function saveEdits() {
    var edits = [];
    (state.data.sections || []).forEach(function (section) {
      var host = document.querySelector('.editable[data-section-id="' + section.id + '"]');
      if (!host) return;
      var after = serializeSection(host);
      if (after.replace(/\s+$/, "") !== (section.markdown || "").replace(/\s+$/, "")) {
        edits.push({ section_id: section.id, before: section.markdown, after: after });
      }
    });
    if (!edits.length) {
      state.dirty = false;
      refreshSaveBtn();
      return;
    }
    state.dirty = false;
    state.pendingPayload = null;
    refreshSaveBtn();
    emit({ action: "save_edits", edits: edits });
  }

  /* ---------------- line numbers ---------------- */
  function paintLineNumbers() {
    var paper = document.querySelector(".paper");
    var gutter = document.querySelector(".gutter");
    if (!paper || !gutter) return;
    gutter.innerHTML = "";
    if (!state.lineNumbers) return;
    var baseTop = paper.getBoundingClientRect().top;
    var count = 0;
    Array.prototype.forEach.call(paper.querySelectorAll("[data-linehost]"), function (host) {
      var rects = [];
      if (host.tagName === "TR") {
        rects = [host.getBoundingClientRect()];
      } else {
        var range = document.createRange();
        range.selectNodeContents(host);
        rects = Array.prototype.slice.call(range.getClientRects());
      }
      var seen = {};
      rects.forEach(function (rect) {
        if (rect.height < 4 || rect.width < 1) return;
        var top = Math.round(rect.top - baseTop);
        if (seen[top]) return;
        seen[top] = true;
        count += 1;
        var label = el("span", "ln", String(count));
        label.style.top = top + "px";
        gutter.appendChild(label);
      });
    });
  }

  /* ---------------- rail ---------------- */
  function renderRail() {
    var rail = el("aside", "rail");
    var head = el("div", "rail-head");
    head.appendChild(document.createTextNode("Comments"));
    var open = (state.data.annotations || []).filter(function (a) { return !a.resolved; }).length;
    head.appendChild(el("span", "count", String(open)));
    head.appendChild(el("span", "spacer-flex"));
    var toggle = el("label", "who small");
    var cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = state.showResolved;
    cb.style.marginRight = "4px";
    cb.addEventListener("change", function () {
      state.showResolved = cb.checked;
      var old = document.querySelector(".rail");
      if (old) old.replaceWith(renderRail());
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
    card.appendChild(el("div", "quote", "“" + (a.quote || "section note") + "”"));
    var who = el("div", "who");
    var dot = el("span", "dot");
    dot.style.cssText =
      "width:14px;height:14px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;" +
      "font-size:8px;color:#fff;background:" + a.color;
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
        row.appendChild(el("b", "", r.author + ": "));
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
      note.appendChild(el("b", "", numberFor(a.id) + ". " + a.author));
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

  function leafOf(node) {
    var element = node && node.nodeType === 1 ? node : node && node.parentElement;
    return element ? element.closest("[data-start]") : null;
  }

  function bindSelection() {
    var paper = document.querySelector(".paper");
    if (!paper) return;
    paper.addEventListener("mouseup", function () {
      if (!state.data.can_annotate || state.mode !== "view") return;
      setTimeout(captureSelection, 0);
    });
    // highlights and revision marks are clickable (blocks are re-created on every
    // render, so the handler lives on the paper instead of each span)
    paper.addEventListener("click", function (event) {
      if (state.mode !== "view") return;
      var span = event.target.closest(".ann");
      if (span && span.dataset.commentId) {
        event.stopPropagation();
        openThread(span.dataset.commentId, span);
      }
    });
    document.addEventListener("mousedown", function (event) {
      if (!event.target.closest(".selbar") && !event.target.closest(".ann")) hideSelbar();
      if (!event.target.closest(".popover") && !event.target.closest(".ann") && !event.target.closest(".card")) {
        hidePopover();
      }
      if (!event.target.closest(".tbl-picker")) {
        var picker = document.querySelector(".tbl-picker");
        if (picker) picker.remove();
      }
    });
  }

  function captureSelection() {
    var sel = window.getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount) { hideSelbar(); return; }
    var range = sel.getRangeAt(0);
    var startLeaf = leafOf(range.startContainer);
    var endLeaf = leafOf(range.endContainer);
    if (!startLeaf || !endLeaf) { hideSelbar(); return; }
    var startSection = startLeaf.closest(".sec");
    var endSection = endLeaf.closest(".sec");
    if (!startSection || startSection !== endSection) { hideSelbar(); return; }
    var section = sectionById(startSection.dataset.sectionId);
    if (!section) { hideSelbar(); return; }
    var start = Number(startLeaf.dataset.start) + offsetIn(startLeaf, range.startContainer, range.startOffset);
    var end = Number(endLeaf.dataset.start) + offsetIn(endLeaf, range.endContainer, range.endOffset);
    if (end < start) { var swap = start; start = end; end = swap; }
    if (end - start < 1) { hideSelbar(); return; }
    var quote = (section.plain || "").slice(start, end);
    if (!quote.trim()) { hideSelbar(); return; }
    var rect = range.getBoundingClientRect();
    state.selection = { sectionId: section.id, start: start, end: end, quote: quote, rect: rect };
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
      var swatch = document.createElement("button");
      swatch.className = "sw" + (color === state.activeColor ? " active" : "");
      swatch.style.background = color;
      swatch.title = "Annotate in this colour";
      swatch.addEventListener("click", function (event) {
        event.stopPropagation();
        state.activeColor = color;
        showSelbar();
      });
      row.appendChild(swatch);
    });
    bar.appendChild(row);

    var quoteText = state.selection.quote;
    bar.appendChild(el("div", "quote", "“" + quoteText.slice(0, 160) + (quoteText.length > 160 ? "…" : "") + "”"));

    var ta = document.createElement("textarea");
    ta.placeholder = mode === "comment" ? "Write your comment…" : "Replace the selected text (leave empty to delete it)";
    if (mode === "change") ta.value = quoteText;
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
    if (mode === "change") {
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
      var value = ta.value;
      var sel = state.selection;
      if (mode === "comment") {
        if (!value.trim()) { ta.focus(); return; }
        emit({
          action: "comment",
          section_id: sel.sectionId,
          start: sel.start,
          end: sel.end,
          quote: sel.quote,
          message: value.trim(),
          color: state.activeColor
        });
      } else {
        if (!value.trim() && !window.confirm("Delete the selected text?")) return;
        emit({
          action: "apply_range",
          section_id: sel.sectionId,
          start: sel.start,
          end: sel.end,
          replacement: value
        });
      }
      hideSelbar();
      var s2 = window.getSelection();
      if (s2) s2.removeAllRanges();
    });
    actions.appendChild(submit);
    bar.appendChild(actions);

    bar.appendChild(el("div", "hint", mode === "comment"
      ? "Your comment is anchored to the selected words"
      : "Reviewers will see this as a tracked change"));
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
    openPopover(id, anchorEl.getBoundingClientRect());
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
    dot.style.cssText =
      "width:16px;height:16px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;" +
      "font-size:8px;color:#fff;background:" + annotation.color;
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
      row.appendChild(el("b", "", reply.author));
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
    var left = rect.right ? rect.right + 12 : rect.left;
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
  window.__reviewState = state; // exposed for the Playwright smoke test
  send("streamlit:componentReady", { apiVersion: 1 });
  resize();
  if (window.ResizeObserver) new ResizeObserver(function () { resize(); }).observe(app);
  window.addEventListener("resize", function () { resize(); scheduleLines(); });
  document.addEventListener("keydown", function (event) {
    if (state.mode !== "edit") return;
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
      event.preventDefault();
      if (state.dirty) saveEdits();
    }
  });
})();
