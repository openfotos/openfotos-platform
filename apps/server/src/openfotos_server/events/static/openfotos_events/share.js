"use strict";

function shareCardText(card) {
  const url = card.querySelector("[data-share-url]").textContent.trim();
  const pin = card.querySelector("[data-share-pin]").textContent.trim();
  return `OneNodeAI Studio private gallery\n${url}\nPIN: ${pin}\n`;
}

function enableCredentialCard() {
  const card = document.querySelector("[data-credential-card]");
  if (!card) return;
  const status = card.querySelector("[data-copy-status]");
  card.querySelector("[data-copy-share]").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(shareCardText(card));
      status.textContent = "Link and PIN copied.";
    } catch {
      status.textContent = "Copy was blocked by the browser. Select the details above manually.";
    }
  });
  card.querySelector("[data-download-share]").addEventListener("click", () => {
    const objectUrl = URL.createObjectURL(new Blob([shareCardText(card)], { type: "text/plain" }));
    const download = document.createElement("a");
    download.href = objectUrl;
    download.download = "onenodeai-studio-private-gallery.txt";
    download.click();
    URL.revokeObjectURL(objectUrl);
  });
}

enableCredentialCard();
