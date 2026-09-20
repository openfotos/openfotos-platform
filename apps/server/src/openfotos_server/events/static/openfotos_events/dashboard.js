"use strict";

function dashboardFragment(name, root = document) {
  return root.querySelector(`[data-dashboard-fragment="${name}"]`);
}

async function submitDashboardForm(form, submitter) {
  const fragmentNames = form.dataset.dashboardFragments.split(",");
  const formData = new FormData(form);
  if (submitter?.name) formData.append(submitter.name, submitter.value);
  if (submitter) submitter.disabled = true;
  form.setAttribute("aria-busy", "true");

  try {
    const response = await fetch(form.action, {
      method: form.method,
      body: formData,
      credentials: "same-origin",
      headers: { "X-OpenFotos-Fragments": fragmentNames.join(",") },
    });
    if (!response.ok) {
      window.location.reload();
      return;
    }
    const parsed = new DOMParser().parseFromString(await response.text(), "text/html");
    const replacements = fragmentNames.map((name) => ({
      current: dashboardFragment(name),
      next: dashboardFragment(name, parsed),
    }));
    if (replacements.some(({ current, next }) => !current || !next)) {
      window.location.reload();
      return;
    }
    for (const { current, next } of replacements) {
      current.replaceWith(document.importNode(next, true));
    }
    if (response.redirected) history.replaceState({}, "", response.url);
  } catch {
    window.location.reload();
  } finally {
    if (form.isConnected) {
      form.removeAttribute("aria-busy");
      if (submitter) submitter.disabled = false;
    }
  }
}

document.addEventListener("submit", (event) => {
  const form = event.target.closest("form[data-dashboard-fragments]");
  if (!form) return;
  event.preventDefault();
  submitDashboardForm(form, event.submitter);
});

document.addEventListener("click", (event) => {
  if (event.target.closest("[data-open-publish-dialog]")) {
    document.querySelector("[data-publish-dialog]")?.showModal();
  }
  if (event.target.closest("[data-close-publish-dialog]")) {
    document.querySelector("[data-publish-dialog]")?.close();
  }
});
