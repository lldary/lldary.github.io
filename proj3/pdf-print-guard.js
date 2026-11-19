// Headless Chromium can race mdbook-pdf with mdBook's own print.html auto-print.
// Keep interactive browser printing unchanged.
if (window.location.pathname.endsWith('/print.html') && navigator.webdriver) {
    window.print = function () {};
}
