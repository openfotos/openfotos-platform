"use strict";

function openPrivateLink() {
  const landing = document.querySelector("[data-share-landing]");
  if (!landing) return;
  const values = new URLSearchParams(window.location.hash.slice(1));
  const secret = values.get("secret");
  window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
  if (!secret) {
    landing.querySelector("[data-share-status]").textContent =
      "This link is incomplete. Ask the sender for the complete private link.";
    return;
  }
  landing.querySelector("[data-share-secret]").value = secret;
  landing.querySelector("[data-share-present-form]").submit();
}

function shareCardText(card) {
  const url = card.querySelector("[data-share-url]").textContent.trim();
  const pin = card.querySelector("[data-share-pin]").textContent.trim();
  return `OpenFotos private gallery\n${url}\nPIN: ${pin}\n`;
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
    download.download = "openfotos-private-gallery.txt";
    download.click();
    URL.revokeObjectURL(objectUrl);
  });
}

openPrivateLink();
enableCredentialCard();
