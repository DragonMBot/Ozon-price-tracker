/* Firefox MV3 event-page background script. */
'use strict';

browser.action.onClicked.addListener(async () => {
  const dashboardUrl = browser.runtime.getURL('ui/index.html');
  try {
    const tabs = await browser.tabs.query({});
    const existing = tabs.find((tab) => tab.url === dashboardUrl);
    if (existing && existing.id !== undefined) {
      await browser.tabs.update(existing.id, { active: true });
      if (existing.windowId !== undefined) {
        await browser.windows.update(existing.windowId, { focused: true });
      }
      return;
    }
    await browser.tabs.create({ url: dashboardUrl });
  } catch (error) {
    console.error('[Ozon Price Tracker] Cannot open dashboard:', error);
  }
});
