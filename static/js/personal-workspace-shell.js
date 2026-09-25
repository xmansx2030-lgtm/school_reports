(function () {
  'use strict';

  var drawer = document.getElementById('mobileDrawer');
  var overlay = document.getElementById('drawerOverlay');
  var trigger = document.getElementById('hamburger');
  var closeButton = document.getElementById('drawerClose');
  var header = document.getElementById('siteHeader');
  if (!drawer || !overlay || !trigger || !closeButton) return;

  var previousFocus = null;

  function closeDrawer() {
    if (!drawer.classList.contains('open')) return;
    drawer.classList.remove('open');
    drawer.setAttribute('aria-hidden', 'true');
    drawer.setAttribute('inert', '');
    overlay.classList.remove('show');
    overlay.setAttribute('aria-hidden', 'true');
    trigger.setAttribute('aria-expanded', 'false');
    trigger.classList.remove('active');
    document.body.classList.remove('personal-drawer-open');
    if (previousFocus && previousFocus.isConnected) previousFocus.focus();
  }

  function openDrawer() {
    previousFocus = document.activeElement;
    drawer.removeAttribute('inert');
    drawer.classList.add('open');
    drawer.setAttribute('aria-hidden', 'false');
    overlay.classList.add('show');
    overlay.setAttribute('aria-hidden', 'false');
    trigger.setAttribute('aria-expanded', 'true');
    trigger.classList.add('active');
    document.body.classList.add('personal-drawer-open');
    closeButton.focus();
  }

  trigger.addEventListener('click', function () {
    if (drawer.classList.contains('open')) closeDrawer();
    else openDrawer();
  });
  closeButton.addEventListener('click', closeDrawer);
  overlay.addEventListener('click', closeDrawer);
  document.addEventListener('keydown', function (event) {
    if (!drawer.classList.contains('open')) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      closeDrawer();
      return;
    }
    if (event.key !== 'Tab') return;
    var focusable = Array.from(drawer.querySelectorAll('a[href], button:not([disabled])'));
    if (!focusable.length) return;
    var first = focusable[0];
    var last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });
  window.addEventListener('resize', function () {
    if (window.innerWidth >= 1280) closeDrawer();
  }, { passive: true });
  window.addEventListener('scroll', function () {
    if (header) header.classList.toggle('scrolled', window.scrollY > 4);
  }, { passive: true });
})();
