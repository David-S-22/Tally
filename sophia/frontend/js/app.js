// Everything below binds to Bills' own root rather than to document or
// document.body, and that is load-bearing rather than tidiness.
//
// The shared shell (:3000) swaps this whole document into its #content, and
// htmx re-executes scripts in swapped content -- so this file runs there,
// against the shell's htmx instance, which every other feature's tab shares.
// Anything set globally here would silently change behaviour for Transactions,
// Anomalies and Savings, and nothing ever sets it back. Scoping to #bills-root
// also means a second visit to the Bills tab rebinds a fresh subtree instead of
// stacking a duplicate listener on a document that is never replaced.
var billsRoot = document.getElementById("bills-root");
if (!billsRoot) {
  throw new Error("#bills-root is missing -- Bills' handlers cannot bind.");
}

// Bills' write routes answer invalid input with a 422 and an HTML error
// fragment, so error responses have to swap into the target instead of being
// discarded. htmx's default drops 4xx/5xx bodies and raises htmx:responseError.
//
// This used to be done with htmx.config.responseHandling, which is global: in
// the shell that one assignment would make every feature swap error bodies and
// stop firing htmx:responseError. The same result is achieved per-event here,
// on Bills' subtree only. 204 and 2xx/3xx are left to htmx's defaults, which
// already match what the old config asked for -- 4xx/5xx was the only
// difference, so behaviour standalone is unchanged.
billsRoot.addEventListener("htmx:beforeSwap", function (evt) {
  var xhr = evt.detail && evt.detail.xhr;
  if (xhr && xhr.status >= 400) {
    evt.detail.shouldSwap = true;
    evt.detail.isError = false;
  }
});

// "Confirm this change" says nothing about what the change is. Controls that
// know carry data-confirm; the rest fall back to the old wording rather than
// guessing, because a confidently wrong summary on a confirmation dialog is
// worse than a vague one.
function confirmPromptFor(elt) {
  var described = elt && elt.closest("[data-confirm]");
  return (described && described.getAttribute("data-confirm")) || "Confirm this change";
}

function showModal(message, onConfirm) {
  // The confirm dialog must NOT live in #modal-root: the add/edit/cancel/
  // payment/dispute forms render there, and replacing them detaches the
  // form so htmx drops the request it is asking to confirm.
  var root = document.getElementById("confirm-root");
  if (!root) {
    root = document.createElement("div");
    root.id = "confirm-root";
    // Into Bills' root, not document.body: in the shell, body is outside the
    // #content the shell replaces on a tab switch, so a dialog appended there
    // would outlive Bills and sit over whichever feature came next.
    billsRoot.appendChild(root);
  }
  var previouslyFocused = document.activeElement;
  root.innerHTML =
    '<div class="modal-backdrop">' +
    '<div class="modal" role="dialog" aria-modal="true" aria-labelledby="confirm-dialog-title">' +
    '<p id="confirm-dialog-title"></p>' +
    '<button type="button" class="confirm">Confirm</button> ' +
    '<button type="button" class="cancel">Cancel</button>' +
    "</div></div>";
  // textContent, not string concatenation: the message can carry a bill name.
  root.querySelector("#confirm-dialog-title").textContent = message || "Confirm this change";

  var close = function () {
    root.innerHTML = "";
    document.removeEventListener("keydown", onKey, true);
    if (previouslyFocused && previouslyFocused.focus) {
      previouslyFocused.focus();
    }
  };
  var onKey = function (e) {
    if (e.key === "Escape") {
      e.preventDefault();
      close();
    }
  };
  document.addEventListener("keydown", onKey, true);
  root.querySelector(".modal-backdrop").addEventListener("click", function (e) {
    if (e.target === e.currentTarget) {
      close();
    }
  });
  root.querySelector(".confirm").addEventListener("click", function () {
    close();
    onConfirm();
  });
  root.querySelector(".cancel").addEventListener("click", close);
  root.querySelector(".confirm").focus();
}

function showToast(text) {
  var root = document.getElementById("toast-root");
  root.innerHTML = '<div class="toast">' + text + "</div>";
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(function () {
    if (root.firstChild) {
      root.innerHTML = "";
    }
  }, 2500);
}

// The page is a single scroll now — no inner tabs. The old tab machinery
// (activateTab, hash-remembered tab state) went with them; the "#bills?confirm"
// and "#chat?" deep links below are contract and survive.

// The add/edit/cancel/payment/dispute forms are server fragments swapped into
// #modal-root, so none of them can carry dialog semantics of their own without
// every template repeating them. Applied here once, when the modal is
// populated: it opens with focus, it announces itself, and Escape or a click on
// the backdrop closes it. Before this it took no focus at all and only the
// Cancel button could close it, which made every form mouse-only.
var modalRoot = document.getElementById("modal-root");
var focusBeforeModal = null;

function closeModalForm() {
  if (!modalRoot.children.length) {
    return;
  }
  modalRoot.innerHTML = "";
  if (focusBeforeModal && focusBeforeModal.focus) {
    focusBeforeModal.focus();
  }
  focusBeforeModal = null;
}

billsRoot.addEventListener("htmx:beforeRequest", function (evt) {
  // Remember what opened it, so focus has somewhere to return to.
  if (evt.detail.target === modalRoot) {
    focusBeforeModal = document.activeElement;
  }
  // Every item in a row menu opens a modal form. Left open, the menu is still
  // sitting there when the form closes, over a row the user stopped thinking
  // about two clicks ago.
  if (evt.detail.elt && evt.detail.elt.closest(".row-menu")) {
    closeRowMenus();
  }
});

// The row action menus are <details>, which means their open state belongs to
// the browser and, more usefully, to the server: no fragment ever ships the
// `open` attribute, so every one of the eight paths that replaces #bills-table
// resets all twelve menus on its own. There is deliberately no state saved here
// and no htmx:afterSwap hook restoring it. That is the entire reason a
// disclosure was chosen over a scripted popup -- there is nothing to go stale.
function closeRowMenus(except) {
  billsRoot.querySelectorAll(".row-menu[open]").forEach(function (menu) {
    if (menu !== except) {
      menu.open = false;
    }
  });
}

// Only one menu open at a time. `toggle` does not bubble, but it does still
// traverse the capture phase -- which is what makes a single delegated listener
// on billsRoot possible here instead of binding twelve rows and rebinding them
// after every swap.
billsRoot.addEventListener("toggle", function (evt) {
  var opened = evt.target;
  if (opened && opened.matches && opened.matches(".row-menu[open]")) {
    closeRowMenus(opened);
  }
}, true);

new MutationObserver(function () {
  var form = modalRoot.querySelector(".modal-form");
  if (!form || form.dataset.dialogReady) {
    return;
  }
  form.dataset.dialogReady = "1";
  form.setAttribute("role", "dialog");
  form.setAttribute("aria-modal", "true");
  var heading = form.querySelector("h3");
  if (heading) {
    if (!heading.id) {
      heading.id = "modal-form-title";
    }
    form.setAttribute("aria-labelledby", heading.id);
  }
  if (!form.hasAttribute("tabindex")) {
    form.setAttribute("tabindex", "-1");
  }
  // First field if there is one, otherwise the dialog itself -- never leave
  // focus behind on the trigger.
  var first = form.querySelector("input:not([type=hidden]), select, textarea, button");
  (first || form).focus();
}).observe(modalRoot, { childList: true });

modalRoot.addEventListener("click", function (evt) {
  // #modal-root is the backdrop once it holds a form -- see the :has() rule in
  // bills.css. A click that lands on it rather than on the form is a click
  // outside the dialog.
  if (evt.target === modalRoot) {
    closeModalForm();
  }
});

// Escape has an order of precedence, top layer first: the confirm dialog, then
// an open row menu, then the modal form underneath them both. Each returns
// early, so one press closes exactly one thing.
//
// Still bound to document rather than billsRoot, unlike everything else in this
// file. Moving it means Escape only fires when focus is inside #bills-root,
// which is true on the paths that matter but not provably true on all of them,
// and a demo is a bad place to find the exception. The cost is that a shell tab
// revisit stacks another listener; each stale one closes over a detached
// modalRoot whose children.length is 0, so they are inert rather than wrong.
// Moves with the inline-confirm work, where this function is rewritten anyway.
document.addEventListener("keydown", function (evt) {
  if (evt.key !== "Escape") {
    return;
  }
  // The confirm dialog sits above everything and owns Escape while it is open.
  if (document.querySelector(".modal-backdrop")) {
    return;
  }
  var openMenu = billsRoot.querySelector(".row-menu[open]");
  if (openMenu) {
    evt.preventDefault();
    openMenu.open = false;
    // Focus goes back to the control that opened it, not to nowhere.
    var toggle = openMenu.querySelector(".row-menu-toggle");
    if (toggle && toggle.focus) {
      toggle.focus();
    }
    return;
  }
  if (modalRoot.children.length) {
    evt.preventDefault();
    closeModalForm();
  }
}, true);

billsRoot.addEventListener("htmx:confirm", function (evt) {
  var verb = (evt.detail.verb || "get").toLowerCase();
  if (verb === "get") {
    return;
  }
  // Two exemptions, for two different reasons. Everything else that writes is
  // still gated.
  //
  // The ask form: asking Tally a question is a POST, but it only ever appends
  // to chat_messages -- no bill, payment or dispute is touched until the change
  // is confirmed. Gating the question made the app announce a change it was not
  // making.
  //
  // The preview card: the card IS the confirmation step. It names the change in
  // prose and the user has to click Confirm on it deliberately. A dialog on top
  // of that asks twice for one decision, and the second ask is strictly the
  // worse of the two -- it has no more information than the card just showed,
  // so it renders the generic "Confirm this change". An earlier version of this
  // comment argued the card had to stay gated because it sits in the same panel
  // as the ask form; that reasoning was about where the control lives, not what
  // it does, and the card was already a review step by the time it was written.
  // The suggestion card (chat copy or panel copy) is the same review step the
  // preview card was: it names the change field by field and Approve/Reject
  // is the deliberate decision. A dialog on top would ask twice with less
  // information than the card already shows.
  if (
    evt.detail.elt &&
    (evt.detail.elt.closest(".chat-panel form") ||
      evt.detail.elt.closest(".preview-card") ||
      evt.detail.elt.closest(".suggestion-card") ||
      // The agent-review block is the same kind of review step: it shows the
      // plan, the draft and the reviewer's risks, and Accept/Reject is the
      // decision. (The "Draft with agents" button that starts a run is not in
      // the block and keeps its data-confirm.)
      evt.detail.elt.closest(".agent-decision") ||
      evt.detail.elt.closest(".tools-panel form"))
  ) {
    return;
  }
  evt.preventDefault();
  showModal(confirmPromptFor(evt.detail.elt), function () {
    evt.detail.issueRequest(true);
  });
});

function markActiveTool(elt) {
  var form = elt && elt.closest ? elt.closest(".tools-panel form") : null;
  if (!form) {
    return;
  }
  var button = elt.matches("button") ? elt : form.querySelector("button[type=submit]");
  form.querySelectorAll("button").forEach(function (b) {
    b.classList.toggle("active", b === button);
  });
}

billsRoot.addEventListener("htmx:beforeRequest", function (evt) {
  markActiveTool(evt.detail.elt);
});

billsRoot.addEventListener("htmx:afterRequest", function (evt) {
  // A modal form used to be wiped as a side effect of the confirm dialog
  // replacing it; now the dialog lives elsewhere, close the form once its
  // request succeeds.
  var modal = document.getElementById("modal-root");
  if (evt.detail.successful && modal && evt.detail.elt && modal.contains(evt.detail.elt)) {
    modal.innerHTML = "";
  }
});

billsRoot.addEventListener("toast", function (evt) {
  var detail = evt.detail || {};
  var text = typeof detail.value === "string" ? detail.value : detail;
  showToast(typeof text === "string" ? text : "Done.");
});

// The Disputes card is collapsed by default: a grid of tiles, with the letter
// panel BELOW them and hidden until a tile is clicked. On a full grid the panel
// therefore opens off-screen, so opening it has to bring it into view.
//
// Only the closed -> open transition scrolls. Every write inside an open panel
// (Mark sent, Regenerate) re-renders it through the same target, and scrolling
// on each of those yanks the page out from under whoever just clicked.
function revealDisputePanel() {
  var panel = document.getElementById("dispute-panel");
  if (panel && !panel.hidden) {
    panel.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
}

var disputePanelWasHidden = false;

billsRoot.addEventListener("htmx:beforeSwap", function (evt) {
  var target = evt.detail && evt.detail.target;
  disputePanelWasHidden = !!(target && target.id === "dispute-panel" && target.hidden);
});

billsRoot.addEventListener("htmx:afterSettle", function () {
  if (disputePanelWasHidden) {
    disputePanelWasHidden = false;
    revealDisputePanel();
  }
});

// Historically switched inner tabs; with the single-page layout the server's
// switchTab trigger (sent when a dispute is created) reveals the freshly opened
// letter instead — same intent, no tabs. Falls back to the section heading when
// the panel did not open, so the card is still found.
billsRoot.addEventListener("switchTab", function () {
  var panel = document.getElementById("dispute-panel");
  if (panel && !panel.hidden) {
    revealDisputePanel();
    return;
  }
  var section = document.getElementById("disputes-section");
  if (section) {
    section.scrollIntoView({ block: "start", behavior: "smooth" });
  }
});

// Client-side search over the rendered rows. No route, no ?q=, no request --
// the whole table is already in the DOM, and filtering it is the honest shape
// for twelve rows. Server-side search is the R1 shape, for when it is not.
//
// Matched against Name and Status. Deliberately not the actions cell, whose
// rows read "Cancel Edit Dispute Record payment" and would match any query
// that is a substring of those words.
var SEARCHABLE_CELLS = [0, 3];

function rowMatches(row, needle) {
  for (var i = 0; i < SEARCHABLE_CELLS.length; i++) {
    var cell = row.cells[SEARCHABLE_CELLS[i]];
    if (cell && cell.textContent.toLowerCase().indexOf(needle) !== -1) {
      return true;
    }
  }
  return false;
}

function applyBillsFilter() {
  var input = document.getElementById("bills-search");
  var table = billsRoot.querySelector(".bills-table");
  if (!input || !table) {
    return;
  }
  var needle = input.value.trim().toLowerCase();
  var rows = table.querySelectorAll("tbody tr[data-bill-id]");
  var shown = 0;
  for (var i = 0; i < rows.length; i++) {
    var hit = !needle || rowMatches(rows[i], needle);
    rows[i].hidden = !hit;
    if (hit) {
      shown++;
    }
  }
  var empty = table.querySelector("tbody tr.no-match");
  if (empty) {
    empty.hidden = !(needle && shown === 0);
  }
}

// Bound on billsRoot rather than the input: the input survives table swaps, but
// binding through the root keeps every listener in this file on one subtree, so
// a second visit to the Bills tab rebinds a fresh subtree instead of stacking a
// duplicate on a document that is never replaced.
billsRoot.addEventListener("input", function (evt) {
  if (evt.target && evt.target.id === "bills-search") {
    applyBillsFilter();
  }
});

// Every write replaces #bills-table wholesale, which brings back rows the filter
// had hidden. Re-apply once the swap has settled.
billsRoot.addEventListener("htmx:afterSwap", function () {
  applyBillsFilter();
});

billsRoot.addEventListener("click", function (evt) {
  // Any click landing outside a menu closes whichever one is open. The <summary>
  // is itself inside .row-menu, so this never fights the browser's own toggle.
  // Deliberately not an early return -- it runs alongside whatever the click was
  // actually for.
  if (!evt.target.closest(".row-menu")) {
    closeRowMenus();
  }

  var rewrite = evt.target.closest('[data-action="rewrite"]');
  if (rewrite) {
    var panel = rewrite.closest(".dispute-panel");
    var feedback = panel && panel.querySelector(".rewrite-feedback");
    if (feedback) {
      feedback.hidden = !feedback.hidden;
    }
    return;
  }

  var copyNote = evt.target.closest('[data-action="copy-note"]');
  if (copyNote) {
    var panelForCopy = copyNote.closest(".dispute-panel");
    var letter = panelForCopy && panelForCopy.querySelector(".letter");
    if (letter && navigator.clipboard) {
      navigator.clipboard.writeText(letter.value).then(function () {
        showToast(copyNote.getAttribute("data-toast") || "Copied");
      });
    }
    return;
  }

  var setAside = evt.target.closest('[data-action="set-aside"]');
  if (setAside) {
    showToast(setAside.getAttribute("data-toast") || "Done — change saved.");
    return;
  }

  var closeDispute = evt.target.closest('[data-action="close-dispute-panel"]');
  if (closeDispute) {
    var panel = document.getElementById("dispute-panel");
    if (panel) {
      panel.innerHTML = "";
      panel.hidden = true;
    }
    return;
  }

  var dismissModal = evt.target.closest('[data-action="dismiss-modal"]');
  if (dismissModal) {
    document.getElementById("modal-root").innerHTML = "";
  }
});

(function () {
  if (location.pathname.indexOf("/handoff/") === 0) {
    htmx.ajax("GET", "/bills-backend/ui" + location.pathname + location.search, { target: "#modal-root", swap: "innerHTML" });
    history.replaceState(null, "", "/");
    return;
  }
  // Canonical form is "?confirm=7#bills" -- parameter in the query, bare page
  // name in the hash, because the shared shell matches the hash EXACTLY and
  // falls back to Home on anything else. The legacy "#bills?confirm=7" form is
  // still read so older links and docs keep working standalone.
  var params = new URLSearchParams(location.search);
  var legacyBills = location.hash.indexOf("#bills?confirm=") === 0;
  if (params.has("confirm") || legacyBills) {
    var billId = parseInt(
      legacyBills ? location.hash.split("confirm=")[1] : params.get("confirm"),
      10
    );
    if (isNaN(billId)) {
      return;
    }
    var scrollToBill = function () {
      var row = document.querySelector('#bills-table tr[data-bill-id="' + billId + '"]');
      if (row) {
        window.clearTimeout(stopLooking);
        billsRoot.removeEventListener("htmx:afterSettle", scrollToBill);
        row.scrollIntoView({ block: "center" });
      }
    };
    var stopLooking = window.setTimeout(function () {
      billsRoot.removeEventListener("htmx:afterSettle", scrollToBill);
    }, 10000);
    billsRoot.addEventListener("htmx:afterSettle", scrollToBill);
    return;
  }
  // Chat intents: "?handoff=<id>#bills" or "?message=<text>#bills". "#chat" is
  // not a shell page at all, so the legacy hash form is standalone-only.
  if (params.has("handoff") || params.has("message") || location.hash.indexOf("#chat?") === 0) {
    var chat = document.querySelector(".ask-tally");
    if (chat) {
      chat.scrollIntoView({ block: "start" });
    }
  }
})();
