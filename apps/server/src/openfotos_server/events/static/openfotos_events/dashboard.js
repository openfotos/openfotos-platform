"use strict";

const publishDialog = document.querySelector("[data-publish-dialog]");
const openPublishDialog = document.querySelector("[data-open-publish-dialog]");
const closePublishDialog = document.querySelector("[data-close-publish-dialog]");

if (publishDialog && openPublishDialog && closePublishDialog) {
  openPublishDialog.addEventListener("click", () => publishDialog.showModal());
  closePublishDialog.addEventListener("click", () => publishDialog.close());
}
