document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".flash-close").forEach((button) => {
    button.addEventListener("click", () => button.closest(".flash").remove());
  });

  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });

  document.querySelectorAll(".file-picker input[type=file]").forEach((input) => {
    input.addEventListener("change", () => {
      const label = input.closest(".file-picker").querySelector("strong");
      label.textContent = input.files.length ? input.files[0].name : "Choose a CSV file";
    });
  });
});
