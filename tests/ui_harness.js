const log = []; const ok = (c, m) => log.push((c ? "PASS " : "FAIL ") + m);
const f = document.getElementById("f");
const key = (d, k) => d.dispatchEvent(new KeyboardEvent("keydown", { key: k, bubbles: true }));
let phase = 0;
f.onload = function () {
  const w = f.contentWindow, d = f.contentDocument, $ = (s) => d.querySelector(s), $$ = (s) => d.querySelectorAll(s);
  setTimeout(() => {
    try {
      if (phase === 0) { try { w.localStorage.clear(); } catch (e) {} phase = 1; w.location.reload(); return; }
      if (phase === 1) {
        ok($$(".row").length === 4, "drafts folder lists 4 conversations");
        $$(".row")[0].click();
        ok(!!$("#subject-h") && $("#subject-h").textContent.includes("Columbus"), "clicking a row opens the reading view");
        $('[data-edit="email"]').click();
        ok($("#compose").classList.contains("open"), "Edit opens the compose window");
        const ta = $("#f-body"); ta.value = ta.value.replace("Congrats", "Big congrats"); ta.dispatchEvent(new Event("input"));
        ok($("#c-send").href.includes("Big%20congrats") && $("#c-send").href.includes("Demo%20Plaza"), "compose send link uses edited text plus footer");
        ok($("#act").href.includes("Big%20congrats"), "reading view action picks up the edit");
        ok($("#edited").classList.contains("show"), "edit is saved");
        key(d, "Escape");
        ok(!$("#compose").classList.contains("open"), "Escape closes compose");
        $("#sent").click();
        ok($("#progtxt").textContent === "1 of 4 ready sent", "mark as sent updates progress");
        ok($("#ledger").textContent.includes("--mark-sent") && $("#ledger").textContent.includes("Northfield Builders"), "ledger command lists the sent target");
        ok($("#subject-h").textContent.includes("Tacoma"), "marking as sent advances to the next draft");
        key(d, "?");
        ok(!$("#keys").hidden, "? opens the shortcuts dialog");
        key(d, "Escape");
        ok($("#keys").hidden, "Escape closes the shortcuts dialog");
        $("#back").click();
        ok($$(".row").length === 3, "sent conversation leaves Drafts");
        $('[data-f="sent"]').click();
        ok($$(".row").length === 1 && $$(".row.read").length === 1, "Sent folder shows it as read");
        $('[data-f="drafts"]').click();
        key(d, "j"); key(d, "o");
        ok($("#subject-h").textContent.includes("Denver"), "j then o opens the next conversation");
        $(".msg.collapsed").click();
        ok($("#act-fu") && $("#act-fu").href.includes("su=Re%3A"), "follow-up expands and opens as a reply");
        key(d, "u");
        const q = $("#q"); q.value = "redstone"; q.dispatchEvent(new Event("input"));
        ok($$(".row").length === 1, "search filters conversations");
        phase = 2; w.location.reload(); return;
      }
      if (phase === 2) {
        ok($("#progtxt").textContent === "1 of 4 ready sent", "sent state persists across reload");
        $('[data-f="all"]').click();
        d.querySelector('.row[data-i="0"]').click();
        ok($("#body-email").textContent.includes("Big congrats"), "edit persists across reload");
        $('[data-edit="email"]').click(); $("#reset").click();
        ok(!$("#body-email").textContent.includes("Big congrats"), "discard restores the reviewed draft");
        $("#back").click(); $('[data-f="drafts"]').click();
        $("#ckall").click(); $("#bulksent").click();
        ok($("#progtxt").textContent === "4 of 4 ready sent", "bulk select marks all as sent");
        $("#snackundo").click();
        ok($("#progtxt").textContent === "1 of 4 ready sent", "undo restores the previous state");
        ok(!d.body.innerText.includes("undefined") && !d.body.innerText.includes("NaN"), "no undefined/NaN rendered");
      }
    } catch (e) { ok(false, "exception: " + e.message); }
    document.getElementById("out").textContent = log.join("\n"); document.title = "DONE";
  }, 300);
};
