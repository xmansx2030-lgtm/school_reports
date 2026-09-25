(function () {
  'use strict';

  var drawer = document.getElementById('mobileDrawer');
  var overlay = document.getElementById('drawerOverlay');
  var trigger = document.getElementById('hamburger');
  var tabbarMore = document.querySelector('[data-mobile-tabbar-more]');
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
    if (tabbarMore) {
      tabbarMore.setAttribute('aria-expanded', 'false');
    }
    document.body.classList.remove('personal-drawer-open');
    var focusTarget = previousFocus && previousFocus.isConnected && previousFocus.getClientRects().length
      ? previousFocus : header && header.querySelector('.hdr-brand');
    if (focusTarget) focusTarget.focus();
  }

  function openDrawer(source) {
    previousFocus = source || document.activeElement;
    drawer.removeAttribute('inert');
    drawer.classList.add('open');
    drawer.setAttribute('aria-hidden', 'false');
    overlay.classList.add('show');
    overlay.setAttribute('aria-hidden', 'false');
    trigger.setAttribute('aria-expanded', 'true');
    trigger.classList.add('active');
    if (tabbarMore) {
      tabbarMore.setAttribute('aria-expanded', 'true');
    }
    document.body.classList.add('personal-drawer-open');
    closeButton.focus();
  }

  trigger.addEventListener('click', function () {
    if (drawer.classList.contains('open')) closeDrawer();
    else openDrawer(trigger);
  });
  if (tabbarMore) {
    tabbarMore.addEventListener('click', function () {
      if (drawer.classList.contains('open')) closeDrawer();
      else openDrawer(tabbarMore);
    });
  }
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
    if (window.innerWidth >= 1280 || (previousFocus === tabbarMore && window.innerWidth > 768)) closeDrawer();
  }, { passive: true });
  window.addEventListener('scroll', function () {
    if (header) header.classList.toggle('scrolled', window.scrollY > 4);
  }, { passive: true });
})();
