const ENDPOINT = "http://127.0.0.1:38953/browser-event";
const tabState = new Map();
const activeByWindow = new Map();

async function postEvent(type, payload) {
  try {
    await fetch(ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        type,
        payload: {
          ...payload,
          browserTimestampMs: Date.now()
        }
      })
    });
  } catch (_) {}
}

function emitTabSnapshot(tab, reason) {
  if (!tab) return;
  postEvent("tab_snapshot", {
    reason,
    tabId: tab.id,
    windowId: tab.windowId,
    url: tab.url || "",
    title: tab.title || "",
    active: !!tab.active
  });
}

chrome.runtime.onInstalled.addListener(() => {
  postEvent("extension_installed", {});
});

chrome.tabs.onCreated.addListener((tab) => {
  tabState.set(tab.id, { windowId: tab.windowId, url: tab.url || "", title: tab.title || "" });
  postEvent("tab_created", {
    tabId: tab.id,
    windowId: tab.windowId,
    url: tab.url || "",
    title: tab.title || ""
  });
});

chrome.tabs.onActivated.addListener(async (info) => {
  let tab = null;
  try {
    tab = await chrome.tabs.get(info.tabId);
  } catch (_) {}
  const prevTabId = activeByWindow.get(info.windowId);
  activeByWindow.set(info.windowId, info.tabId);
  tabState.set(info.tabId, { windowId: info.windowId, url: tab?.url || "", title: tab?.title || "" });
  postEvent("tab_activated", {
    tabId: info.tabId,
    windowId: info.windowId,
    url: tab?.url || "",
    title: tab?.title || ""
  });
  emitTabSnapshot(tab, "tab_activated");
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  const prev = tabState.get(tabId) || {};
  const urlChanged = !!changeInfo.url && changeInfo.url !== (prev.url || "");
  const titleChanged = typeof changeInfo.title === "string" && changeInfo.title !== (prev.title || "");
  tabState.set(tabId, { windowId: tab.windowId, url: tab.url || "", title: tab.title || "" });
  postEvent("tab_updated", {
    tabId,
    windowId: tab.windowId,
    status: changeInfo.status || "",
    url: changeInfo.url || tab.url || "",
    title: tab.title || ""
  });
  emitTabSnapshot(tab, "tab_updated");
  if (urlChanged || titleChanged || changeInfo.status === "complete") {
    emitTabSnapshot(tab, "tab_updated");
  }
});

chrome.tabs.onRemoved.addListener((tabId, removeInfo) => {
  const st = tabState.get(tabId) || {};
  postEvent("tab_closed", {
    tabId,
    windowId: removeInfo.windowId,
    isWindowClosing: !!removeInfo.isWindowClosing,
    url: st.url || "",
    title: st.title || ""
  });
  tabState.delete(tabId);
  if (activeByWindow.get(removeInfo.windowId) === tabId) activeByWindow.delete(removeInfo.windowId);
  postEvent("tab_removed", {
    tabId,
    windowId: removeInfo.windowId,
    isWindowClosing: !!removeInfo.isWindowClosing
  });
});

chrome.tabs.onMoved.addListener((tabId, moveInfo) => {
  postEvent("tab_moved", {
    tabId,
    windowId: moveInfo.windowId,
    fromIndex: moveInfo.fromIndex,
    toIndex: moveInfo.toIndex
  });
});

chrome.windows.onFocusChanged.addListener((windowId) => {
  postEvent("window_focus_changed", { windowId });
});

chrome.webNavigation.onCommitted.addListener((details) => {
  postEvent("navigation_committed", {
    tabId: details.tabId,
    frameId: details.frameId,
    transitionType: details.transitionType || "",
    transitionQualifiers: details.transitionQualifiers || [],
    url: details.url || ""
  });
});

chrome.webNavigation.onBeforeNavigate.addListener((details) => {
  postEvent("before_navigate", {
    tabId: details.tabId,
    frameId: details.frameId,
    url: details.url || ""
  });
});

chrome.runtime.onMessage.addListener((message, sender) => {
  if (!message || message.type !== "browser_input") return;
  if (Number.isFinite(sender?.tab?.id)) {
    tabState.set(sender.tab.id, {
      windowId: sender?.tab?.windowId ?? null,
      url: sender?.tab?.url || "",
      title: sender?.tab?.title || ""
    });
  }
  postEvent("browser_input", {
    tabId: sender?.tab?.id || null,
    windowId: sender?.tab?.windowId || null,
    url: sender?.tab?.url || message.payload?.pageUrl || "",
    title: sender?.tab?.title || "",
    fieldType: message.payload?.fieldType || "",
    fieldName: message.payload?.fieldName || "",
    fieldKey: message.payload?.fieldKey || "",
    valueSample: message.payload?.valueSample || "",
    inputLength: Number(message.payload?.inputLength || 0)
  });
});

setInterval(() => {
  chrome.tabs.query({}, (tabs) => {
    (tabs || []).forEach((tab) => {
      emitTabSnapshot(tab, "periodic");
    });
  });
}, 60000);
