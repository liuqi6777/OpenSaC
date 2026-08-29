const status = document.querySelector("[data-copy-status]");
let statusTimer;

const showStatus = (message) => {
  if (!status) return;
  status.textContent = message;
  status.classList.add("is-visible");
  window.clearTimeout(statusTimer);
  statusTimer = window.setTimeout(() => status.classList.remove("is-visible"), 1600);
};

document.querySelectorAll("[data-copy]").forEach((button) => {
  button.addEventListener("click", async () => {
    const target = document.querySelector(button.dataset.copy);
    const label = button.querySelector("[data-copy-label]");
    if (!target) return;

    try {
      await navigator.clipboard.writeText(target.textContent.trim());
      if (label) label.textContent = "Copied";
      showStatus("Copied to clipboard");
      window.setTimeout(() => {
        if (!label) return;
        label.textContent = button.dataset.copy === "#bibtex" ? "Copy BibTeX" : "Copy";
      }, 1600);
    } catch {
      showStatus("Select the text to copy");
    }
  });
});

document.querySelectorAll("[data-year]").forEach((element) => {
  element.textContent = String(new Date().getFullYear());
});
