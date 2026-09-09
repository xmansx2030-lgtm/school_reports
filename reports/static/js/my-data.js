document.addEventListener("DOMContentLoaded", () => {
  const printButton = document.getElementById("printDataCopy");
  if (printButton) printButton.addEventListener("click", () => window.print());
});
